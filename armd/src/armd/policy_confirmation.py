"""Pi-issued, short-lived, one-shot authorization for one hardware policy request."""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PolicyConfirmation:
    confirmation_id: str
    request_id: str
    session_id: str
    expires_monotonic_ns: int


class PolicyConfirmationManager:
    """Issues opaque confirmations that can authorize exactly one matching request."""

    def __init__(self, *, ttl_s: float = 15.0, clock_ns=time.monotonic_ns) -> None:
        if ttl_s <= 0:
            raise ValueError("policy confirmation ttl must be positive")
        self.ttl_ns = round(ttl_s * 1_000_000_000)
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._pending: dict[str, PolicyConfirmation] = {}

    def issue(self, *, request_id: str, session_id: str) -> PolicyConfirmation:
        request_id = request_id.strip()
        session_id = session_id.strip()
        if not request_id or not session_id:
            raise ValueError("request_id and session_id are required for policy confirmation")
        now = self._clock_ns()
        confirmation = PolicyConfirmation(
            confirmation_id=secrets.token_urlsafe(32),
            request_id=request_id,
            session_id=session_id,
            expires_monotonic_ns=now + self.ttl_ns,
        )
        with self._lock:
            self._purge_expired_locked(now)
            self._pending[confirmation.confirmation_id] = confirmation
        return confirmation

    def consume(self, confirmation_id: str, *, request_id: str, session_id: str) -> bool:
        now = self._clock_ns()
        with self._lock:
            self._purge_expired_locked(now)
            confirmation = self._pending.get(confirmation_id)
            if confirmation is None:
                return False
            if (
                confirmation.request_id != request_id
                or confirmation.session_id != session_id
                or now > confirmation.expires_monotonic_ns
            ):
                return False
            del self._pending[confirmation_id]
            return True

    def revoke_all(self) -> None:
        with self._lock:
            self._pending.clear()

    @staticmethod
    def request_local_issue(
        socket_path: str | Path,
        *,
        request_id: str,
        session_id: str,
        timeout_s: float = 2.0,
    ) -> PolicyConfirmation:
        request = json.dumps({"request_id": request_id, "session_id": session_id}).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_s)
            client.connect(str(Path(socket_path).expanduser()))
            client.sendall(request + b"\n")
            response = b""
            while not response.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        value = json.loads(response.decode("utf-8"))
        if not value.get("issued"):
            raise ValueError(str(value.get("error", "policy confirmation was not issued")))
        return PolicyConfirmation(
            confirmation_id=str(value["confirmation_id"]),
            request_id=request_id,
            session_id=session_id,
            expires_monotonic_ns=int(value["expires_pi_monotonic_ns"]),
        )

    def _purge_expired_locked(self, now: int) -> None:
        expired = [
            confirmation_id
            for confirmation_id, confirmation in self._pending.items()
            if now > confirmation.expires_monotonic_ns
        ]
        for confirmation_id in expired:
            del self._pending[confirmation_id]


class PolicyConfirmationSocket:
    """Unix-socket issuer reachable only by the local armd OS user."""

    def __init__(
        self,
        manager: PolicyConfirmationManager,
        path: str | Path,
        *,
        allowed_uid: int | None = None,
    ) -> None:
        self.manager = manager
        self.path = Path(path).expanduser()
        self.allowed_uid = os.getuid() if allowed_uid is None else int(allowed_uid)
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._server is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = self.path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"policy confirmation path is not a socket: {self.path}")
            self.path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        server.listen(4)
        server.settimeout(0.2)
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="panthera-policy-confirmation",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            server.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._server = None
        self._thread = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                self._handle(connection)

    def _handle(self, connection: socket.socket) -> None:
        try:
            peer_pid, peer_uid, _ = self._peer_credentials(connection)
            if peer_uid != self.allowed_uid:
                raise PermissionError(f"local peer uid {peer_uid} is not authorized")
            if not self._peer_has_tty(peer_pid):
                raise PermissionError("policy confirmation issuer has no local controlling TTY")
            request = b""
            while not request.endswith(b"\n") and len(request) <= 4096:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                request += chunk
            value = json.loads(request.decode("utf-8"))
            confirmation = self.manager.issue(
                request_id=str(value.get("request_id", "")),
                session_id=str(value.get("session_id", "")),
            )
            response = {
                "issued": True,
                "confirmation_id": confirmation.confirmation_id,
                "expires_pi_monotonic_ns": confirmation.expires_monotonic_ns,
            }
        except (OSError, PermissionError, TypeError, ValueError) as exc:
            response = {"issued": False, "error": str(exc)}
        connection.sendall(json.dumps(response).encode("utf-8") + b"\n")

    @staticmethod
    def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
        import struct

        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)

    @staticmethod
    def _peer_has_tty(pid: int) -> bool:
        try:
            tty_number = int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[6])
        except (OSError, ValueError, IndexError):
            return False
        return tty_number != 0
