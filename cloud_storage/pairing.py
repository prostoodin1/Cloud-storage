from __future__ import annotations

import base64
import json
import re
import urllib.parse
import zlib
from dataclasses import dataclass

_FINGERPRINT = re.compile(r"^[0-9a-fA-F]{64}$")
DISCOVERY_PREFIX = "CLOUD_STORAGE_DISCOVER_V1:"
CONNECTION_CODE_PREFIX = "CS1."
SERVER_CODE_PREFIX = "CS2."


@dataclass(frozen=True, slots=True)
class PairingInvitation:
    code: str
    server_url: str = ""
    certificate_fingerprint: str = ""
    username: str = ""
    alternate_addresses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServerLocator:
    server_url: str
    certificate_fingerprint: str = ""
    username: str = ""
    alternate_addresses: tuple[str, ...] = ()

    @property
    def primary_address(self) -> str:
        return self.server_url

    @property
    def addresses(self) -> tuple[str, ...]:
        return (self.server_url, *self.alternate_addresses)


def build_server_code(
    server_url: str,
    certificate_fingerprint: str = "",
    username: str = "",
    *,
    alternate_addresses: list[str] | tuple[str, ...] = (),
) -> str:
    invitation = _validated_invitation(
        "SERVER00", server_url, certificate_fingerprint, username
    )
    alternatives: list[str] = []
    for address in alternate_addresses:
        validated = _validated_invitation("SERVER00", str(address), "", "")
        if validated.server_url != invitation.server_url:
            alternatives.append(validated.server_url)
    alternatives = list(dict.fromkeys(alternatives))[:8]
    payload = json.dumps(
        {
            "s": invitation.server_url,
            "f": invitation.certificate_fingerprint,
            "u": invitation.username,
            "a": alternatives,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(zlib.compress(payload, level=9)).decode("ascii")
    return SERVER_CODE_PREFIX + encoded.rstrip("=")


def parse_server_code(value: str) -> ServerLocator | None:
    candidate = value.strip()
    if not candidate.upper().startswith(SERVER_CODE_PREFIX):
        return None
    encoded = candidate[len(SERVER_CODE_PREFIX) :]
    if not encoded or len(encoded) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise ValueError("invalid Cloud Storage server code")
    try:
        compressed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, 8193)
        if len(raw) > 8192 or decompressor.unconsumed_tail or not decompressor.eof:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError, zlib.error) as exc:
        raise ValueError("invalid Cloud Storage server code") from exc
    if not isinstance(payload, dict) or set(payload) - {"s", "f", "u", "a"}:
        raise ValueError("invalid Cloud Storage server code")
    invitation = _validated_invitation(
        "SERVER00",
        str(payload.get("s", "")),
        str(payload.get("f", "")),
        str(payload.get("u", "")),
    )
    raw_alternatives = payload.get("a", [])
    if not isinstance(raw_alternatives, list) or len(raw_alternatives) > 8:
        raise ValueError("invalid Cloud Storage server code")
    alternatives: list[str] = []
    for address in raw_alternatives:
        validated = _validated_invitation("SERVER00", str(address), "", "")
        if validated.server_url != invitation.server_url:
            alternatives.append(validated.server_url)
    return ServerLocator(
        invitation.server_url,
        invitation.certificate_fingerprint,
        invitation.username,
        tuple(dict.fromkeys(alternatives)),
    )


def build_connection_code(
    code: str,
    server_url: str,
    certificate_fingerprint: str = "",
    username: str = "",
) -> str:
    """Build one opaque code containing everything the desktop client needs."""
    invitation = _validated_invitation(code, server_url, certificate_fingerprint, username)
    payload = json.dumps(
        {
            "c": invitation.code,
            "s": invitation.server_url,
            "f": invitation.certificate_fingerprint,
            "u": invitation.username,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(zlib.compress(payload, level=9)).decode("ascii")
    return CONNECTION_CODE_PREFIX + encoded.rstrip("=")


def parse_connection_code(value: str) -> PairingInvitation | None:
    candidate = value.strip()
    if not candidate.upper().startswith(CONNECTION_CODE_PREFIX):
        return None
    encoded = candidate[len(CONNECTION_CODE_PREFIX) :]
    if not encoded or len(encoded) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise ValueError("invalid Cloud Storage connection code")
    try:
        compressed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, 8193)
        if len(raw) > 8192 or decompressor.unconsumed_tail or not decompressor.eof:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError, zlib.error) as exc:
        raise ValueError("invalid Cloud Storage connection code") from exc
    if not isinstance(payload, dict) or set(payload) - {"c", "s", "f", "u"}:
        raise ValueError("invalid Cloud Storage connection code")
    return _validated_invitation(
        str(payload.get("c", "")),
        str(payload.get("s", "")),
        str(payload.get("f", "")),
        str(payload.get("u", "")),
    )


def build_pairing_uri(
    code: str,
    server_url: str = "",
    certificate_fingerprint: str = "",
    username: str = "",
    *,
    alternate_addresses: list[str] | tuple[str, ...] = (),
) -> str:
    parameters = {"code": code.strip().upper()}
    if server_url:
        parameters["server"] = server_url.strip().rstrip("/")
    fingerprint = certificate_fingerprint.replace(":", "").strip().lower()
    if fingerprint:
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("certificate fingerprint must be 64 hexadecimal characters")
        parameters["fingerprint"] = fingerprint
    clean_username = username.strip()
    if clean_username:
        if len(clean_username) > 128:
            raise ValueError("invalid username")
        parameters["username"] = clean_username
    alternatives: list[str] = []
    for address in alternate_addresses:
        validated = _validated_invitation("SERVER00", str(address), "", "")
        if validated.server_url != server_url.strip().rstrip("/"):
            alternatives.append(validated.server_url)
    if alternatives:
        parameters["alt"] = list(dict.fromkeys(alternatives))[:8]
    return "cloudstorage://pair?" + urllib.parse.urlencode(parameters, doseq=True)


def parse_pairing_uri(value: str) -> PairingInvitation | None:
    candidate = value.strip()
    connection_code = parse_connection_code(candidate)
    if connection_code is not None:
        return connection_code
    if not candidate.casefold().startswith("cloudstorage://"):
        return None
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme.casefold() != "cloudstorage" or parsed.netloc.casefold() != "pair":
        raise ValueError("invalid Cloud Storage invitation")
    parameters = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    code = _single(parameters, "code").strip().upper()
    if not 8 <= len(code) <= 16:
        raise ValueError("invalid pairing code in invitation")
    server_url = _single(parameters, "server", required=False).strip().rstrip("/")
    fingerprint = _single(parameters, "fingerprint", required=False).replace(":", "").lower()
    if fingerprint and not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("invalid certificate fingerprint in invitation")
    username = _single(parameters, "username", required=False).strip()
    if len(username) > 128:
        raise ValueError("invalid username in invitation")
    raw_alternatives = parameters.get("alt", [])
    if len(raw_alternatives) > 8:
        raise ValueError("too many alternate addresses in invitation")
    alternatives: list[str] = []
    for address in raw_alternatives:
        validated = _validated_invitation("SERVER00", address, "", "")
        if validated.server_url != server_url:
            alternatives.append(validated.server_url)
    return PairingInvitation(
        code,
        server_url,
        fingerprint,
        username,
        tuple(dict.fromkeys(alternatives)),
    )


def _validated_invitation(
    code: str,
    server_url: str,
    certificate_fingerprint: str,
    username: str,
) -> PairingInvitation:
    normalized_code = code.strip().upper()
    if not 8 <= len(normalized_code) <= 16:
        raise ValueError("invalid pairing code")
    normalized_url = server_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError("invalid server URL")
    fingerprint = certificate_fingerprint.replace(":", "").strip().lower()
    if fingerprint and not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("invalid certificate fingerprint")
    clean_username = username.strip()
    if len(clean_username) > 128:
        raise ValueError("invalid username")
    return PairingInvitation(normalized_code, normalized_url, fingerprint, clean_username)


def _single(
    values: dict[str, list[str]],
    name: str,
    *,
    required: bool = True,
) -> str:
    items = values.get(name, [])
    if len(items) != 1:
        if required:
            raise ValueError(f"invitation must contain exactly one {name}")
        if items:
            raise ValueError(f"invitation must contain at most one {name}")
        return ""
    return items[0]
