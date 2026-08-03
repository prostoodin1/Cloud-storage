from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

_FINGERPRINT = re.compile(r"^[0-9a-fA-F]{64}$")
DISCOVERY_PREFIX = "CLOUD_STORAGE_DISCOVER_V1:"


@dataclass(frozen=True, slots=True)
class PairingInvitation:
    code: str
    server_url: str = ""
    certificate_fingerprint: str = ""


def build_pairing_uri(
    code: str,
    server_url: str = "",
    certificate_fingerprint: str = "",
) -> str:
    parameters = {"code": code.strip().upper()}
    if server_url:
        parameters["server"] = server_url.strip().rstrip("/")
    fingerprint = certificate_fingerprint.replace(":", "").strip().lower()
    if fingerprint:
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("certificate fingerprint must be 64 hexadecimal characters")
        parameters["fingerprint"] = fingerprint
    return "cloudstorage://pair?" + urllib.parse.urlencode(parameters)


def parse_pairing_uri(value: str) -> PairingInvitation | None:
    candidate = value.strip()
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
    return PairingInvitation(code, server_url, fingerprint)


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
