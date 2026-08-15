from __future__ import annotations

import os
import re
from pathlib import Path


class ManagerPasswordVault:
    """Manager-only password escrow protected by the current OS account.

    Core continues to store only Argon2id hashes.  This vault exists solely so the
    administrator who created or reset a password can copy it again from Manager.
    """

    def __init__(self, data_directory: Path) -> None:
        self.directory = data_directory / "manager-passwords"

    def store(self, user_id: str, password: str) -> None:
        path = self._path(user_id)
        if not 10 <= len(password) <= 256:
            raise ValueError("invalid password length")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = password.encode("utf-8")
        if os.name == "nt":
            import win32crypt

            protected = win32crypt.CryptProtectData(
                payload,
                "Cloud Storage managed user password",
                None,
                None,
                None,
                0,
            )
            payload = protected[1] if isinstance(protected, tuple) else protected
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)

    def load(self, user_id: str) -> str:
        path = self._path(user_id)
        if not path.is_file():
            return ""
        try:
            payload = path.read_bytes()
            if os.name == "nt":
                import win32crypt

                unprotected = win32crypt.CryptUnprotectData(payload, None, None, None, 0)
                payload = unprotected[1] if isinstance(unprotected, tuple) else unprotected
            password = payload.decode("utf-8")
            return password if 10 <= len(password) <= 256 else ""
        except (OSError, UnicodeDecodeError):
            return ""

    def clear(self, user_id: str) -> None:
        self._path(user_id).unlink(missing_ok=True)

    def _path(self, user_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)[:100]
        if not safe:
            raise ValueError("invalid user id")
        return self.directory / f"{safe}.bin"
