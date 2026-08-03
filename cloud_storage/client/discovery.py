from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from cloud_storage.client.settings import validate_server_url
from cloud_storage.pairing import DISCOVERY_PREFIX


@dataclass(frozen=True, slots=True)
class DiscoveredServer:
    server_name: str
    url: str
    fingerprint: str
    server_id: str

    @property
    def display_fingerprint(self) -> str:
        return ":".join(
            self.fingerprint[index : index + 2].upper()
            for index in range(0, len(self.fingerprint), 2)
        )


def discover_servers(port: int = 47777, timeout: float = 1.5) -> list[DiscoveredServer]:
    if not 1024 <= port <= 65535:
        raise ValueError("discovery port must be between 1024 and 65535")
    nonce = secrets.token_hex(16)
    request = (DISCOVERY_PREFIX + nonce).encode("ascii")
    discovered: dict[str, DiscoveredServer] = {}
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    probe.bind(("0.0.0.0", 0))
    try:
        for target in (("255.255.255.255", port), ("127.0.0.1", port)):
            try:
                probe.sendto(request, target)
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe.settimeout(max(0.02, deadline - time.monotonic()))
            try:
                payload, sender = probe.recvfrom(2048)
            except TimeoutError:
                break
            except OSError:
                break
            server = _parse_response(payload, sender[0], nonce)
            if server is not None:
                discovered[server.server_id] = server
    finally:
        probe.close()
    return sorted(discovered.values(), key=lambda item: (item.server_name.casefold(), item.url))


def _parse_response(payload: bytes, sender: str, nonce: str) -> DiscoveredServer | None:
    try:
        sender_address = ipaddress.ip_address(sender)
        value = json.loads(payload.decode("utf-8"))
        url = validate_server_url(str(value["url"]))
        parsed_url = urlsplit(url)
        url_address = ipaddress.ip_address(parsed_url.hostname or "")
        fingerprint = str(value["fingerprint"]).casefold()
        server_id = str(value["server_id"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not (sender_address.is_private or sender_address.is_loopback):
        return None
    if not (url_address.is_private or url_address.is_loopback):
        return None
    if (
        value.get("service") != "cloud-storage"
        or value.get("protocol") != 1
        or value.get("nonce") != nonce
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
        or server_id != fingerprint[:16]
    ):
        return None
    return DiscoveredServer(
        server_name=str(value.get("server_name") or "Cloud Storage")[:80],
        url=url,
        fingerprint=fingerprint,
        server_id=server_id,
    )
