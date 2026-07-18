"""单线程独占硬件的固定周期控制循环。"""

from __future__ import annotations

import enum
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from .backend import Backend, MotorSnapshot

ResultT = TypeVar("ResultT")


class CancelReason(str, enum.Enum):
    CLIENT = "client"
    WATCHDOG = "watchdog"
    FORCE_ACQUIRE = "force_acquire"
    ESTOP = "estop"
    SHUTDOWN = "shutdown"


class MotionStepResult(str, enum.Enum):
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


@runtime_checkable
class SteppableMotion(Protocol):
    """必须在单个控制周期内快速返回的非阻塞运动状态机。"""

    def request_cancel(self, reason: CancelReason) -> None:
        """只切换内部状态；安全减速由后续 `step()` 逐周期完成。"""

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        """推进一个控制周期，严禁内部等待整段运动完成。"""


@dataclass(frozen=True, slots=True)
class CachedRobotState:
    motors: tuple[MotorSnapshot, ...]
    refreshed_at: float

    def age_s(self, now: float) -> float:
        return max(0.0, now - self.refreshed_at)


@dataclass(frozen=True, slots=True)
class LoopStats:
    cycles: int
    actual_hz: float
    overruns: int
    last_cycle_s: float
    max_cycle_s: float


@dataclass(slots=True)
class _BackendCall(Generic[ResultT]):
    operation: Callable[[Backend], ResultT]
    future: Future[ResultT]


@dataclass(slots=True)
class _StartMotion:
    motion: SteppableMotion
    future: Future[MotionStepResult]


class HardwareLoop:
    """唯一创建、持有并调用 `Backend` 的线程。

    周期顺序固定为：EStop → cancel → 刷新状态 → 处理有界命令队列 →
    推进活动运动一步 → 更新周期统计。所有外部 I/O 请求必须经 `submit()`
    marshal 到该线程，禁止任何旁路直接碰后端。
    """

    def __init__(
        self,
        backend_factory: Callable[[], Backend],
        *,
        control_hz: float = 200.0,
        max_calls_per_cycle: int = 32,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if control_hz <= 0:
            raise ValueError("control_hz 必须为正数")
        if max_calls_per_cycle <= 0:
            raise ValueError("max_calls_per_cycle 必须为正整数")
        self.control_hz = control_hz
        self.period_s = 1.0 / control_hz
        self._backend_factory = backend_factory
        self._max_calls_per_cycle = max_calls_per_cycle
        self._clock = clock
        self._sleeper = sleeper
        self._requests: queue.SimpleQueue[_BackendCall[object] | _StartMotion] = queue.SimpleQueue()
        self._stop_requested = threading.Event()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._state_lock = threading.Lock()
        self._estop_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._failure: BaseException | None = None
        self._cached_state: CachedRobotState | None = None
        self._estop_engaged = False
        self._estop_applied = False
        self._cancel_reason: CancelReason | None = None
        self._active_motion: SteppableMotion | None = None
        self._motion_future: Future[MotionStepResult] | None = None
        self._stats = LoopStats(0, 0.0, 0, 0.0, 0.0)

    @property
    def is_running(self) -> bool:
        return self._started.is_set() and not self._stopped.is_set()

    @property
    def thread_id(self) -> int | None:
        return self._thread_id

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def estop_engaged(self) -> bool:
        with self._estop_lock:
            return self._estop_engaged

    @property
    def estop_applied(self) -> bool:
        with self._estop_lock:
            return self._estop_applied

    @property
    def has_active_motion(self) -> bool:
        return self._active_motion is not None

    def start(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            raise RuntimeError("HardwareLoop 只能启动一次")
        self._thread = threading.Thread(target=self._run, name="panthera-hardware-loop", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise TimeoutError("HardwareLoop 启动超时")
        if self._failure is not None:
            raise RuntimeError("HardwareLoop 启动失败") from self._failure

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_requested.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("HardwareLoop 停止超时")

    def submit(self, operation: Callable[[Backend], ResultT]) -> Future[ResultT]:
        self._require_running()
        future: Future[ResultT] = Future()
        self._requests.put(_BackendCall(operation=operation, future=future))
        return future

    def start_motion(self, motion: SteppableMotion) -> Future[MotionStepResult]:
        self._require_running()
        future: Future[MotionStepResult] = Future()
        self._requests.put(_StartMotion(motion=motion, future=future))
        return future

    def request_cancel(self, reason: CancelReason = CancelReason.CLIENT) -> None:
        self._require_running()
        with self._state_lock:
            self._cancel_reason = reason

    def request_estop(self) -> None:
        self._require_running()
        with self._estop_lock:
            self._estop_engaged = True
            self._estop_applied = False

    def clear_estop(self) -> bool:
        """仅清除已由 HardwareLoop 实际下发过 stop 的 latch。"""
        self._require_running()
        with self._estop_lock:
            if not self._estop_engaged or not self._estop_applied:
                return False
            self._estop_engaged = False
            self._estop_applied = False
            return True

    def latest_state(self) -> CachedRobotState | None:
        with self._state_lock:
            return self._cached_state

    def stats(self) -> LoopStats:
        with self._stats_lock:
            return self._stats

    def wait_for_cycles(self, minimum: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stats().cycles >= minimum:
                return True
            if self._stopped.wait(min(self.period_s, 0.01)):
                break
        return self.stats().cycles >= minimum

    def _run(self) -> None:
        backend: Backend | None = None
        started_at = self._clock()
        next_tick = started_at
        cycles = 0
        overruns = 0
        max_cycle_s = 0.0
        try:
            backend = self._backend_factory()
            self._thread_id = threading.get_ident()
            backend.refresh_state()
            self._cache_state(backend, self._clock())
            self._started.set()

            while not self._stop_requested.is_set():
                cycle_started = self._clock()
                estop = self._apply_estop_if_needed(backend)
                if not estop:
                    self._apply_cancel_if_needed()

                backend.refresh_state()
                self._cache_state(backend, self._clock())

                if not estop:
                    self._process_requests(backend)
                    self._step_motion(backend, self._clock())

                cycle_s = self._clock() - cycle_started
                cycles += 1
                max_cycle_s = max(max_cycle_s, cycle_s)
                next_tick += self.period_s
                sleep_s = next_tick - self._clock()
                if sleep_s <= 0:
                    overruns += 1
                    missed = int((-sleep_s) // self.period_s) + 1
                    next_tick += missed * self.period_s
                else:
                    self._sleeper(sleep_s)
                elapsed = max(self._clock() - started_at, 1e-12)
                with self._stats_lock:
                    self._stats = LoopStats(cycles, cycles / elapsed, overruns, cycle_s, max_cycle_s)
        except BaseException as exc:
            self._failure = exc
            self._started.set()
        finally:
            if backend is not None:
                self._shutdown_backend(backend)
            self._fail_pending_requests()
            self._stopped.set()

    def _apply_estop_if_needed(self, backend: Backend) -> bool:
        with self._estop_lock:
            engaged = self._estop_engaged
            applied = self._estop_applied
        if not engaged:
            return False
        if not applied:
            backend.stop()
            self._finish_motion(MotionStepResult.CANCELLED)
            with self._estop_lock:
                self._estop_applied = True
        return True

    def _apply_cancel_if_needed(self) -> None:
        if self._active_motion is None:
            return
        with self._state_lock:
            reason = self._cancel_reason
            self._cancel_reason = None
        if reason is not None:
            self._active_motion.request_cancel(reason)

    def _process_requests(self, backend: Backend) -> None:
        for _ in range(self._max_calls_per_cycle):
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            if isinstance(request, _StartMotion):
                if self._active_motion is not None:
                    request.future.set_exception(RuntimeError("已有运动正在执行"))
                else:
                    self._active_motion = request.motion
                    self._motion_future = request.future
                continue
            if request.future.cancelled():
                continue
            try:
                request.future.set_result(request.operation(backend))
            except BaseException as exc:
                request.future.set_exception(exc)

    def _step_motion(self, backend: Backend, now: float) -> None:
        motion = self._active_motion
        if motion is None:
            return
        try:
            result = motion.step(backend, now)
        except BaseException as exc:
            future = self._motion_future
            self._active_motion = None
            self._motion_future = None
            if future is not None and not future.done():
                future.set_exception(exc)
            return
        if result is not MotionStepResult.RUNNING:
            self._finish_motion(result)

    def _finish_motion(self, result: MotionStepResult) -> None:
        future = self._motion_future
        self._active_motion = None
        self._motion_future = None
        if future is not None and not future.done():
            future.set_result(result)

    def _cache_state(self, backend: Backend, refreshed_at: float) -> None:
        state = CachedRobotState(tuple(backend.read_all()), refreshed_at)
        with self._state_lock:
            self._cached_state = state

    def _shutdown_backend(self, backend: Backend) -> None:
        try:
            if self._active_motion is not None:
                self._active_motion.request_cancel(CancelReason.SHUTDOWN)
            backend.stop()
        finally:
            try:
                self._finish_motion(MotionStepResult.CANCELLED)
            finally:
                backend.close()

    def _fail_pending_requests(self) -> None:
        error = RuntimeError("HardwareLoop 已停止")
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            if not request.future.done():
                request.future.set_exception(error)

    def _require_running(self) -> None:
        if not self.is_running:
            raise RuntimeError("HardwareLoop 尚未运行或已经停止")
