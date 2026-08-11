from __future__ import annotations

from collections.abc import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance:
    """One GUI process per logged-in OS user, with activation forwarding."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.server = QLocalServer()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._receive_activation)
        self._activation_handler: Callable[[], None] | None = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._notify_existing():
            return False
        QLocalServer.removeServer(self.name)
        if self.server.listen(self.name):
            self._acquired = True
            return True
        if self._notify_existing():
            return False
        raise RuntimeError(f"cannot create the single-instance endpoint: {self.name}")

    def set_activation_handler(self, handler: Callable[[], None]) -> None:
        self._activation_handler = handler

    def close(self) -> None:
        if self._acquired:
            self.server.close()
            QLocalServer.removeServer(self.name)
            self._acquired = False

    def _notify_existing(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(250):
            return False
        socket.write(b"activate")
        socket.flush()
        socket.waitForBytesWritten(250)
        socket.disconnectFromServer()
        return True

    def _receive_activation(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is not None:
                socket.waitForReadyRead(100)
                socket.readAll()
                socket.disconnectFromServer()
            if self._activation_handler is not None:
                self._activation_handler()

    def __enter__(self) -> SingleInstance:
        if not self.acquire():
            raise RuntimeError("another application instance is already running")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
