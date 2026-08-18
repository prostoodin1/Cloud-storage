"""Google OAuth 2.0 for Gmail SMTP, using PKCE and a loopback callback."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def authorize_gmail(client_id: str, *, timeout_seconds: int = 300) -> str:
    """Return a refresh token; no Google password is handled by Cloud Storage."""
    client_id = client_id.strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("Укажите OAuth Client ID типа …apps.googleusercontent.com")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}
    arrived = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result.update({key: values[0] for key, values in query.items() if values})
            arrived.set()
            body = "<h2>Cloud Storage</h2><p>Вход завершён. Можно вернуться в Manager.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        query = urllib.parse.urlencode(
            {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": "https://mail.google.com/", "access_type": "offline", "prompt": "consent",
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        )
        if not webbrowser.open("https://accounts.google.com/o/oauth2/v2/auth?" + query):
            raise RuntimeError("Не удалось открыть браузер для входа Google")
        if not arrived.wait(timeout_seconds):
            raise TimeoutError("Время ожидания входа Google истекло")
        if result.get("state") != state or "error" in result or not result.get("code"):
            raise RuntimeError("Google не подтвердил вход")
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urllib.parse.urlencode(
                {"client_id": client_id, "code": result["code"], "code_verifier": verifier,
                 "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
            ).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            token = json.loads(response.read().decode("utf-8"))
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise RuntimeError("Google не выдал refresh token; повторите вход и подтвердите доступ")
        return refresh_token
    finally:
        server.shutdown()
        server.server_close()


def authorize_google_identity(client_id: str, *, timeout_seconds: int = 300) -> str:
    """Open Google sign-in and return a short-lived OpenID identity token."""
    client_id = client_id.strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("Google-вход не настроен администратором сервера")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}
    arrived = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result.update({key: values[0] for key, values in query.items() if values})
            arrived.set()
            body = "<h2>Cloud Storage</h2><p>Вход завершён. Можно вернуться в Client.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        if not webbrowser.open("https://accounts.google.com/o/oauth2/v2/auth?" + query):
            raise RuntimeError("Не удалось открыть браузер для входа Google")
        if not arrived.wait(timeout_seconds):
            raise TimeoutError("Время ожидания входа Google истекло")
        if result.get("state") != state or "error" in result or not result.get("code"):
            raise RuntimeError("Google не подтвердил вход")
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "code": result["code"],
                    "code_verifier": verifier,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                }
            ).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            token = json.loads(response.read().decode("utf-8"))
        identity = str(token.get("id_token") or "")
        if not identity:
            raise RuntimeError("Google не выдал подтверждение личности")
        return identity
    finally:
        server.shutdown()
        server.server_close()
