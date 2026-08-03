from __future__ import annotations

import ipaddress
import json
import socket
import threading

from cloud_storage.core.config import CoreConfig
from cloud_storage.core.tls import TlsIdentity
from cloud_storage.pairing import DISCOVERY_PREFIX


class DiscoveryResponder:
    def __init__(self, config: CoreConfig, identity: TlsIdentity) -> None:
        self.config = config
        self.identity = identity
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="cloud-storage-discovery",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket = listener
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("0.0.0.0", self.config.discovery_port))
            listener.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    payload, sender = listener.recvfrom(512)
                except TimeoutError:
                    continue
                except OSError:
                    break
                self._respond(listener, payload, sender)
        finally:
            listener.close()
            self._socket = None

    def _respond(
        self,
        listener: socket.socket,
        payload: bytes,
        sender: tuple[str, int],
    ) -> None:
        try:
            message = payload.decode("ascii")
            address = ipaddress.ip_address(sender[0])
        except (UnicodeDecodeError, ValueError):
            return
        if not message.startswith(DISCOVERY_PREFIX) or len(message) != len(DISCOVERY_PREFIX) + 32:
            return
        if not (address.is_private or address.is_loopback):
            return
        nonce = message[len(DISCOVERY_PREFIX) :]
        if any(character not in "0123456789abcdef" for character in nonce):
            return
        local_address = _route_address(sender[0])
        response = json.dumps(
            {
                "service": "cloud-storage",
                "protocol": 1,
                "nonce": nonce,
                "server_name": self.config.server_name,
                "url": f"https://{local_address}:{self.config.lan_port}",
                "fingerprint": self.identity.fingerprint,
                "server_id": self.identity.fingerprint[:16],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            listener.sendto(response, sender)
        except OSError:
            return


def _route_address(remote_address: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((remote_address, 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()
