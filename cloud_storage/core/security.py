from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
_PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class InvalidCredential(ValueError):
    pass


@dataclass(slots=True)
class CredentialService:
    hmac_secret: bytes
    _password_hasher: PasswordHasher = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=2,
            hash_len=32,
            salt_len=16,
        )

    @classmethod
    def from_secret(cls, secret: str) -> CredentialService:
        return cls(hmac_secret=bytes.fromhex(secret))

    @staticmethod
    def validate_username(username: str) -> str:
        normalized = username.strip().casefold()
        if not _USERNAME.fullmatch(normalized):
            raise InvalidCredential(
                "username must be 3-32 ASCII characters: letters, digits, dot, dash or underscore"
            )
        return normalized

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 10 or len(password) > 256:
            raise InvalidCredential("password must contain between 10 and 256 characters")
        if password.casefold() in {"password123", "1234567890", "qwerty12345"}:
            raise InvalidCredential("password is too common")

    def hash_password(self, password: str) -> str:
        self.validate_password(password)
        return self._password_hasher.hash(password)

    def verify_password(self, stored_hash: str, password: str) -> bool:
        try:
            return self._password_hasher.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def generate_pairing_code(self) -> str:
        raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(8))
        return f"{raw[:4]}-{raw[4:]}"

    @staticmethod
    def normalize_pairing_code(code: str) -> str:
        compact = code.strip().upper().replace("-", "").replace(" ", "")
        if len(compact) != 8 or any(char not in _PAIRING_ALPHABET for char in compact):
            raise InvalidCredential("invalid pairing code")
        return compact

    def fingerprint(self, value: str, purpose: str) -> str:
        return hmac.new(
            self.hmac_secret,
            f"{purpose}\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def pairing_code_hash(self, code: str) -> str:
        return self.fingerprint(self.normalize_pairing_code(code), "pairing-code")

    def generate_device_token(self) -> str:
        return "csd_" + secrets.token_urlsafe(48)

    def device_token_hash(self, token: str) -> str:
        if not token.startswith("csd_") or len(token) < 50:
            raise InvalidCredential("invalid device token")
        return self.fingerprint(token, "device-token")

    def issue_remote_session(
        self,
        user_id: str,
        device_id: str,
        *,
        password_version: int = 0,
        ttl_seconds: int = 24 * 60 * 60,
    ) -> tuple[str, int]:
        now = int(time.time())
        expires_at = now + max(300, min(ttl_seconds, 7 * 24 * 60 * 60))
        payload = urlsafe_b64encode(
            json.dumps(
                {
                    "v": 1,
                    "uid": user_id,
                    "did": device_id,
                    "pv": password_version,
                    "iat": now,
                    "exp": expires_at,
                    "nonce": secrets.token_urlsafe(12),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = self.fingerprint(payload, "remote-session")
        return f"css_{payload}.{signature}", expires_at

    def verify_remote_session(
        self,
        token: str,
        *,
        user_id: str,
        device_id: str,
        password_version: int = 0,
    ) -> None:
        if not token.startswith("css_") or len(token) > 4096:
            raise InvalidCredential("internet login is required")
        try:
            payload, signature = token[4:].split(".", 1)
        except ValueError as exc:
            raise InvalidCredential("internet login is required") from exc
        expected = self.fingerprint(payload, "remote-session")
        if not self.constant_time_equal(signature, expected):
            raise InvalidCredential("internet login is required")
        try:
            padding = "=" * (-len(payload) % 4)
            claims = json.loads(urlsafe_b64decode(payload + padding).decode("utf-8"))
            valid = (
                claims.get("v") == 1
                and claims.get("uid") == user_id
                and claims.get("did") == device_id
                and int(claims.get("pv", 0)) == password_version
                and int(claims.get("exp", 0)) > int(time.time())
                and int(claims.get("iat", 0)) <= int(time.time()) + 60
            )
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidCredential("internet login is required") from exc
        if not valid:
            raise InvalidCredential("internet login is required")

    @staticmethod
    def constant_time_equal(left: str, right: str) -> bool:
        return hmac.compare_digest(left.encode(), right.encode())
