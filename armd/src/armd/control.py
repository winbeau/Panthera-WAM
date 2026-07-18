"""控制权 lease 与心跳状态。"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

LEASE_METADATA_KEY = "x-panthera-lease"


@dataclass(frozen=True, slots=True)
class LeaseSnapshot:
    held: bool
    holder_client_id: str
    token: str
    heartbeat_age_s: float
    watchdog_ok: bool


@dataclass(frozen=True, slots=True)
class AcquireResult:
    granted: bool
    holder_client_id: str
    token: str
    replaced_holder: bool = False


@dataclass(slots=True)
class _Lease:
    client_id: str
    token: str
    last_heartbeat: float


class LeaseManager:
    def __init__(self, *, timeout_s: float = 2.0, clock=time.monotonic) -> None:
        if timeout_s <= 0:
            raise ValueError("lease timeout 必须为正数")
        self.timeout_s = timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._lease: _Lease | None = None

    def acquire(self, client_id: str, *, force: bool = False) -> AcquireResult:
        client_id = client_id.strip()
        if not client_id:
            raise ValueError("client_id 不能为空")
        now = self._clock()
        with self._lock:
            current = self._lease
            stale = current is not None and now - current.last_heartbeat > self.timeout_s
            if current is not None and current.client_id == client_id:
                if stale:
                    token = secrets.token_urlsafe(32)
                    self._lease = _Lease(client_id, token, now)
                    return AcquireResult(True, client_id, token, replaced_holder=True)
                current.last_heartbeat = now
                return AcquireResult(True, client_id, current.token)
            if current is not None and not stale and not force:
                return AcquireResult(False, current.client_id, "")
            replaced = current is not None
            token = secrets.token_urlsafe(32)
            self._lease = _Lease(client_id, token, now)
            return AcquireResult(True, client_id, token, replaced_holder=replaced)

    def validate(self, token: str) -> bool:
        now = self._clock()
        with self._lock:
            return (
                self._lease is not None
                and now - self._lease.last_heartbeat <= self.timeout_s
                and secrets.compare_digest(self._lease.token, token)
            )

    def heartbeat(self, token: str) -> bool:
        now = self._clock()
        with self._lock:
            if (
                self._lease is None
                or now - self._lease.last_heartbeat > self.timeout_s
                or not secrets.compare_digest(self._lease.token, token)
            ):
                return False
            self._lease.last_heartbeat = now
            return True

    def release(self, token: str) -> bool:
        now = self._clock()
        with self._lock:
            if (
                self._lease is None
                or now - self._lease.last_heartbeat > self.timeout_s
                or not secrets.compare_digest(self._lease.token, token)
            ):
                return False
            self._lease = None
            return True

    def expire_if_stale(self) -> LeaseSnapshot | None:
        now = self._clock()
        with self._lock:
            if self._lease is None or now - self._lease.last_heartbeat <= self.timeout_s:
                return None
            expired = self._snapshot_locked(now)
            self._lease = None
            return expired

    def snapshot(self) -> LeaseSnapshot:
        now = self._clock()
        with self._lock:
            return self._snapshot_locked(now)

    def _snapshot_locked(self, now: float) -> LeaseSnapshot:
        if self._lease is None:
            return LeaseSnapshot(False, "", "", 0.0, True)
        age = max(0.0, now - self._lease.last_heartbeat)
        return LeaseSnapshot(True, self._lease.client_id, self._lease.token, age, age <= self.timeout_s)
