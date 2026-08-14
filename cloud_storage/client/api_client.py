from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any, Never

from cloud_storage.client.settings import validate_server_url


class ClientConnectionError(ConnectionError):
    pass


class ClientApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CertificateMismatch(ClientConnectionError):
    pass


class TransferInterrupted(ClientConnectionError):
    pass


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, certificate_fingerprint: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.certificate_fingerprint = certificate_fingerprint

    def connect(self) -> None:
        super().connect()
        if self.sock is None:
            raise ClientConnectionError("TLS connection has no socket")
        certificate = self.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(certificate).hexdigest().casefold()
        if actual != self.certificate_fingerprint:
            self.close()
            raise CertificateMismatch("server certificate fingerprint does not match")


class ClientApi:
    def __init__(
        self,
        server_url: str,
        token: str | None = None,
        certificate_fingerprint: str = "",
        remote_session: str | None = None,
    ) -> None:
        self.server_url = validate_server_url(server_url)
        self.token = token
        self.certificate_fingerprint = certificate_fingerprint.replace(":", "").casefold()
        self.remote_session = remote_session
        if self.certificate_fingerprint and (
            len(self.certificate_fingerprint) != 64
            or any(
                character not in "0123456789abcdef" for character in self.certificate_fingerprint
            )
        ):
            raise ValueError("certificate fingerprint must be 64 hexadecimal characters")
        self._parsed = urllib.parse.urlsplit(self.server_url)

    def health(self) -> dict[str, Any]:
        return self._json_request("/v1/health", timeout=2.0)

    def redeem_invitation(
        self,
        code: str,
        password: str | None,
        device_name: str,
        platform: str,
    ) -> dict[str, Any]:
        return self._json_request(
            "/v1/pairing/redeem",
            method="POST",
            payload={
                "code": code,
                "password": password or None,
                "device_name": device_name,
                "platform": platform,
            },
            timeout=10.0,
        )

    def login_new_device(
        self,
        username: str,
        password: str,
        device_name: str,
        platform: str,
    ) -> dict[str, Any]:
        return self._json_request(
            "/v1/auth/device-login",
            method="POST",
            payload={
                "username": username,
                "password": password,
                "device_name": device_name,
                "platform": platform,
            },
            timeout=10.0,
        )

    def pairing_status(self) -> dict[str, Any]:
        return self._json_request("/v1/pairing/status")

    def create_remote_session(self, username: str, password: str) -> dict[str, Any]:
        return self._json_request(
            "/v1/remote/session",
            method="POST",
            payload={"username": username, "password": password},
            timeout=10.0,
        )

    def list_spaces(self) -> list[dict[str, Any]]:
        return self._json_request("/v1/spaces")

    def list_entries(self, space_id: str, directory: str = "") -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"directory": directory})
        return self._json_request(
            f"/v1/spaces/{urllib.parse.quote(space_id, safe='')}/entries?{query}"
        )

    def create_directory(self, space_id: str, logical_path: str) -> dict[str, Any]:
        return self._json_request(
            f"/v1/spaces/{urllib.parse.quote(space_id, safe='')}/directories",
            method="POST",
            payload={"logical_path": logical_path},
        )

    def delete_directory(self, space_id: str, logical_path: str) -> bool:
        result = self._json_request(
            f"/v1/spaces/{urllib.parse.quote(space_id, safe='')}/directories/"
            f"{urllib.parse.quote(logical_path, safe='/')}",
            method="DELETE",
        )
        return bool(result.get("deleted"))

    def move_entry(
        self,
        space_id: str,
        source_path: str,
        destination_path: str,
        kind: str,
    ) -> dict[str, Any]:
        return self._json_request(
            f"/v1/spaces/{urllib.parse.quote(space_id, safe='')}/moves",
            method="POST",
            payload={
                "source_path": source_path,
                "destination_path": destination_path,
                "kind": kind,
            },
        )

    def upload_file(
        self,
        space_id: str,
        logical_path: str,
        source: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        size = source.stat().st_size
        route = self._file_route(space_id, logical_path)
        connection = self._connection(timeout=30.0)
        headers = self._headers()
        headers.update(
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
                "Accept": "application/json",
            }
        )
        try:
            connection.putrequest("PUT", route)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            sent = 0
            with source.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, size)
            response = connection.getresponse()
            body = response.read()
            if response.status >= 400:
                self._raise_api_error(response.status, body)
            return json.loads(body.decode("utf-8"))
        except CertificateMismatch:
            raise
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise ClientConnectionError("server connection failed during upload") from exc
        finally:
            connection.close()

    def download_file(
        self,
        space_id: str,
        logical_path: str,
        destination: Path,
        progress: Callable[[int, int], None] | None = None,
        *,
        expected_sha256: str = "",
        should_continue: Callable[[], bool] | None = None,
    ) -> Path:
        route = self._file_route(space_id, logical_path)
        connection = self._connection(timeout=30.0)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        existing = temporary.stat().st_size if temporary.exists() else 0
        headers = self._headers()
        if existing:
            headers["Range"] = f"bytes={existing}-"
        try:
            connection.request("GET", route, headers=headers)
            response = connection.getresponse()
            if response.status >= 400:
                self._raise_api_error(response.status, response.read())
            append = existing > 0 and response.status == 206
            received = existing if append else 0
            content_range = response.getheader("Content-Range", "")
            if "/" in content_range:
                total = int(content_range.rsplit("/", 1)[1])
            else:
                total = received + int(response.getheader("Content-Length", "0"))
            if progress:
                progress(received, total)
            with temporary.open("ab" if append else "wb") as handle:
                while chunk := response.read(1024 * 1024):
                    if should_continue is not None and not should_continue():
                        raise TransferInterrupted("transfer paused")
                    handle.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_sha256:
                digest = hashlib.sha256()
                with temporary.open("rb") as handle:
                    while chunk := handle.read(4 * 1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest().casefold() != expected_sha256.casefold():
                    temporary.unlink(missing_ok=True)
                    raise ClientConnectionError("downloaded file SHA-256 does not match")
            os.replace(temporary, destination)
            return destination
        except (CertificateMismatch, TransferInterrupted):
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise ClientConnectionError("server connection failed during download") from exc
        finally:
            connection.close()

    def delete_file(self, space_id: str, logical_path: str) -> dict[str, Any]:
        return self._json_request(self._file_route(space_id, logical_path), method="DELETE")

    def create_resumable_upload(
        self,
        space_id: str,
        logical_path: str,
        size_bytes: int,
        *,
        content_type: str = "application/octet-stream",
        sha256: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "logical_path": logical_path,
            "size_bytes": size_bytes,
            "content_type": content_type,
        }
        if sha256:
            payload["sha256"] = sha256
        return self._json_request(
            f"/v1/spaces/{urllib.parse.quote(space_id, safe='')}/uploads",
            method="POST",
            payload=payload,
            timeout=10.0,
        )

    def resumable_upload_status(self, upload_id: str) -> dict[str, Any]:
        return self._json_request(f"/v1/uploads/{urllib.parse.quote(upload_id, safe='')}")

    def append_resumable_upload(
        self,
        upload_id: str,
        offset: int,
        payload: bytes,
    ) -> dict[str, Any]:
        if not payload:
            raise ValueError("upload chunk cannot be empty")
        return self._json_request(
            f"/v1/uploads/{urllib.parse.quote(upload_id, safe='')}",
            method="PATCH",
            body=payload,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": str(offset),
            },
            timeout=30.0,
        )

    def complete_resumable_upload(self, upload_id: str) -> dict[str, Any]:
        return self._json_request(
            f"/v1/uploads/{urllib.parse.quote(upload_id, safe='')}/complete",
            method="POST",
            timeout=30.0,
        )

    def cancel_resumable_upload(self, upload_id: str) -> bool:
        result = self._json_request(
            f"/v1/uploads/{urllib.parse.quote(upload_id, safe='')}",
            method="DELETE",
        )
        return bool(result.get("cancelled"))

    def _json_request(
        self,
        route: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        if payload is not None and body is not None:
            raise ValueError("request cannot contain both JSON payload and raw body")
        request_body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else body
        )
        request_headers = self._headers()
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        connection = self._connection(timeout=timeout)
        try:
            connection.request(method, route, body=request_body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            if response.status >= 400:
                self._raise_api_error(response.status, content)
            return json.loads(content.decode("utf-8")) if content else None
        except (ClientApiError, CertificateMismatch):
            raise
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise ClientConnectionError("server is not reachable") from exc
        finally:
            connection.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.remote_session:
            headers["X-Cloud-Remote-Session"] = self.remote_session
        return headers

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        host = self._parsed.hostname or ""
        if self._parsed.scheme == "https":
            if self.certificate_fingerprint:
                return PinnedHTTPSConnection(
                    host,
                    self._parsed.port or 443,
                    timeout=timeout,
                    context=ssl._create_unverified_context(),  # noqa: SLF001 - exact pin below
                    certificate_fingerprint=self.certificate_fingerprint,
                )
            return http.client.HTTPSConnection(
                host,
                self._parsed.port or 443,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(host, self._parsed.port or 80, timeout=timeout)

    def _file_route(self, space_id: str, logical_path: str) -> str:
        return (
            f"/v1/spaces/{urllib.parse.quote(space_id, safe='')}/files/"
            f"{urllib.parse.quote(logical_path, safe='/')}"
        )

    @staticmethod
    def _raise_api_error(status_code: int, body: bytes) -> Never:
        try:
            detail = json.loads(body.decode("utf-8")).get("detail", "request failed")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            detail = "request failed"
        raise ClientApiError(status_code, str(detail))
