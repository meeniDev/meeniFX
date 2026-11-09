"""One running editor per user.

Windows starts a new process for every shortcut click, every file-association activation and
every installer relaunch. Without a guard, each process creates its own start window, so a
few clicks look like the application is duplicating itself. The first process owns a
per-user local socket; later processes forward an activation (and an optional project path)
to it and exit immediately.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


def server_name():
    """A stable, per-user socket name; another Windows account is its own instance."""
    identity = os.path.expanduser("~") or getpass.getuser() or "meeniFX"
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:20]
    return f"meeniFX-Studios-{digest}"


class SingleInstance(QObject):
    """Claim the started instance or wake the one that already owns the socket."""

    activation_requested = pyqtSignal(str)

    def __init__(self, name=None, parent=None):
        super().__init__(parent)
        self.name = name or server_name()
        self._owns_server = False
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)

    def acquire(self, project_path=""):
        """True when this process is the first one; False after waking the first one."""
        if self._send(project_path):
            return False
        # A crashed process can leave the named pipe behind. Only remove it after a real
        # connection failed, never while another instance is answering.
        QLocalServer.removeServer(self.name)
        if self._server.listen(self.name):
            self._owns_server = True
            return True
        # Two starts raced between the failed connection and listen(): let the winner run.
        return not self._send(project_path)

    def close(self):
        if not self._owns_server:
            return
        self._owns_server = False
        self._server.close()
        QLocalServer.removeServer(self.name)

    def _send(self, project_path):
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(250):
            socket.abort()
            socket.deleteLater()
            return False
        payload = json.dumps({"path": str(project_path or "")}, ensure_ascii=False).encode("utf-8")
        if socket.write(payload) < 0:
            socket.abort()
            socket.deleteLater()
            return False
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        socket.deleteLater()
        return True

    def _accept(self):
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            if connection is None:
                break
            # The client writes before disconnecting. A short wait lets the complete small
            # JSON payload arrive without splitting it across readyRead events.
            connection.waitForReadyRead(300)
            raw = bytes(connection.readAll()).strip()
            path = ""
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    path = str(payload.get("path") or "")
                except (TypeError, ValueError, UnicodeDecodeError):
                    path = ""
            self.activation_requested.emit(path)
            connection.disconnectFromServer()
            connection.deleteLater()
