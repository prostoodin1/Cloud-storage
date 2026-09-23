"""Same-origin browser access; no device credential is exposed to JavaScript."""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr

from cloud_storage.core.repository import NotFoundError

COOKIE = "cloud_storage_session"
SESSION_SECONDS = 30 * 24 * 3600
ASSETS = Path(__file__).with_name("web_assets")


class BrowserPairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=8192)
    remember: bool = False


class BrowserLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: SecretStr
    remember: bool = False


class BrowserAccess:
    def __init__(self, runtime):
        self.runtime = runtime

    def fingerprint(self, value: str, purpose: str = "web-session") -> str:
        return self.runtime.repository.credentials.fingerprint(value, purpose)

    @staticmethod
    def secure(request: Request) -> bool:
        return (
            request.url.scheme == "https"
            or bool(getattr(request.state, "external_request", False))
            or bool(getattr(request.state, "lan_request", False))
        )

    def allowed(self, request: Request) -> None:
        if self.runtime.control.settings()["browser_access"] == "nobody":
            raise HTTPException(403, "Вход через браузер отключён администратором")
        if not self.secure(request) and request.url.hostname not in {
            "127.0.0.1", "localhost", "::1", "testserver"
        }:
            raise HTTPException(403, "Для браузерного входа требуется HTTPS")

    def check_origin(self, request: Request) -> None:
        # Do not trust Forwarded/X-Forwarded-* supplied by an arbitrary client.
        scheme = "https" if self.secure(request) else "http"
        expected = f"{scheme}://{request.url.netloc}"
        if request.headers.get("origin", "").rstrip("/") != expected:
            raise HTTPException(403, "Запрос с другого сайта запрещён")
        if request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
            raise HTTPException(403, "Запрос с другого сайта запрещён")

    def authenticate(self, request: Request):
        self.allowed(request)
        token = request.cookies.get(COOKIE, "")
        if not token or len(token) > 256:
            raise HTTPException(401, "Введите новый код подключения из Manager")
        with self.runtime.database.connection() as connection:
            row = connection.execute(
                "SELECT device_id FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
                (self.fingerprint(token), int(time.time())),
            ).fetchone()
        if row is None:
            raise HTTPException(401, "Сеанс завершён. Введите новый код подключения")
        try:
            device = self.runtime.repository.get_device(row["device_id"])
            user = self.runtime.repository.get_user(device.user_id)
        except NotFoundError as exc:
            raise HTTPException(401, "Доступ отозван администратором") from exc
        if device.status != "trusted" or not user.enabled or device.pairing_method != "dynamic":
            raise HTTPException(403, "Доступ отозван администратором")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.check_origin(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(csrf, self.fingerprint(token, "web-csrf")):
                raise HTTPException(403, "Проверка безопасности не пройдена. Обновите страницу")
        return device

    def _start_session(
        self,
        request: Request,
        device_id: str,
        *,
        remember: bool,
        payload: dict | None = None,
    ) -> JSONResponse:
        token = secrets.token_urlsafe(48)
        old = request.cookies.get(COOKIE, "")
        with self.runtime.database.transaction() as connection:
            connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (int(time.time()),))
            if old:
                connection.execute(
                    "DELETE FROM web_sessions WHERE token_hash = ?", (self.fingerprint(old),)
                )
            connection.execute(
                "INSERT INTO web_sessions(token_hash, device_id, expires_at) VALUES(?, ?, ?)",
                (self.fingerprint(token), device_id, int(time.time()) + SESSION_SECONDS),
            )
        content = {
            "device_id": device_id,
            "csrf": self.fingerprint(token, "web-csrf"),
            **(payload or {}),
        }
        response = JSONResponse(content)
        cookie_options = {
            "httponly": True,
            "secure": self.secure(request),
            "samesite": "strict",
            "path": "/",
        }
        if remember:
            cookie_options["max_age"] = SESSION_SECONDS
        response.set_cookie(COOKIE, token, **cookie_options)
        return response

    def register(self, app, redeem, redeem_type):
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(ASSETS / "index.html", media_type="text/html")

        @app.get("/web/assets/{name}", include_in_schema=False)
        def asset(name: str):
            allowed = {
                "app.js": "text/javascript",
                "app.css": "text/css",
                "account.css": "text/css",
            }
            if name not in allowed:
                raise HTTPException(404, "not found")
            return FileResponse(ASSETS / name, media_type=allowed[name])

        @app.post("/v1/web/pair", tags=["browser"])
        def pair(body: BrowserPairRequest, request: Request):
            self.allowed(request)
            self.check_origin(request)
            if not body.code.strip().upper().startswith("CS3."):
                raise HTTPException(422, "Нужен текущий динамический код CS3 из Manager")
            result = redeem(
                redeem_type(code=body.code, device_name="Веб-браузер", platform="Web"), request
            )
            response = self._start_session(
                request,
                result["device"]["id"],
                remember=body.remember,
                payload={
                    "device": result["device"],
                    "initial_username": result.get("initial_username", ""),
                    "initial_password": result.get("initial_password", ""),
                },
            )
            return response

        @app.post("/v1/web/login", tags=["browser"])
        def login(body: BrowserLoginRequest, request: Request):
            self.allowed(request)
            self.check_origin(request)
            user = self.runtime.repository.authenticate_user_password(
                body.username, body.password.get_secret_value()
            )
            result = self.runtime.repository.create_trusted_browser_device(
                user_id=user.id,
                remote_address=request.client.host if request.client else "unknown",
            )
            return self._start_session(request, result.device.id, remember=body.remember)

        @app.get("/v1/web/session", tags=["browser"])
        def session(request: Request):
            device = self.authenticate(request)
            return {
                "device": asdict(device),
                "csrf": self.fingerprint(request.cookies[COOKIE], "web-csrf"),
                "server_name": self.runtime.config.server_name,
            }

        @app.post("/v1/web/logout", tags=["browser"])
        def logout(request: Request):
            # Logout also works after revocation; Origin still prevents login/logout CSRF.
            self.check_origin(request)
            with self.runtime.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM web_sessions WHERE token_hash = ?",
                    (self.fingerprint(request.cookies.get(COOKIE, "")),),
                )
            response = JSONResponse({"logged_out": True})
            response.delete_cookie(COOKIE, httponly=True, secure=self.secure(request), samesite="strict")
            return response
