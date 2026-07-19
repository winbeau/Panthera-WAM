"""长动作 execution 注册表。"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Protocol

from .hardware_loop import CancelReason, MotionStepResult


class TrackedMotion(Protocol):
    reject_reason: str

    @property
    def fraction(self) -> float: ...

    def request_cancel(self, reason: CancelReason) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    execution_id: str
    result: MotionStepResult
    fraction: float
    error_message: str
    terminal: bool


@dataclass(slots=True)
class _ExecutionRecord:
    execution_id: str
    motion: TrackedMotion
    completion: Future[MotionStepResult]


class ExecutionRegistry:
    def __init__(self) -> None:
        self._records: dict[str, _ExecutionRecord] = {}
        self._lock = threading.Lock()

    def register(
        self,
        motion: TrackedMotion,
        completion: Future[MotionStepResult],
    ) -> str:
        execution_id = uuid.uuid4().hex
        with self._lock:
            self._records[execution_id] = _ExecutionRecord(execution_id, motion, completion)
        return execution_id

    def cancel(self, execution_id: str, reason: CancelReason = CancelReason.CLIENT) -> bool:
        with self._lock:
            record = self._records.get(execution_id)
        if record is None or record.completion.done():
            return False
        record.motion.request_cancel(reason)
        return True

    def snapshot(self, execution_id: str) -> ExecutionSnapshot | None:
        with self._lock:
            record = self._records.get(execution_id)
        if record is None:
            return None
        if not record.completion.done():
            return ExecutionSnapshot(
                execution_id=execution_id,
                result=MotionStepResult.RUNNING,
                fraction=record.motion.fraction,
                error_message="",
                terminal=False,
            )
        try:
            result = record.completion.result()
            error = record.motion.reject_reason
        except BaseException as exc:
            result = MotionStepResult.FAILED
            error = str(exc)
        return ExecutionSnapshot(
            execution_id=execution_id,
            result=result,
            fraction=record.motion.fraction,
            error_message=error,
            terminal=True,
        )
