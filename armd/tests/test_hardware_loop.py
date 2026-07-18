from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from armd.backend import Backend, FrameMode, JointFrame, SimBackend
from armd.hardware_loop import CancelReason, HardwareLoop, MotionStepResult


def velocity_frame(value: float) -> JointFrame:
    return JointFrame(
        mode=FrameMode.VELOCITY,
        arm_position=np.zeros(6),
        arm_velocity=np.array([value, 0.0, 0.0, 0.0, 0.0, 0.0]),
        gripper_position=0.0,
        gripper_velocity=0.0,
    )


class ThreadRecordingBackend(SimBackend):
    def __init__(self) -> None:
        super().__init__()
        self.call_threads: set[int] = set()
        self.stop_event = threading.Event()
        self.idle_maintain_count = 0

    def refresh_state(self) -> None:
        self.call_threads.add(threading.get_ident())
        super().refresh_state()

    def read_all(self):
        self.call_threads.add(threading.get_ident())
        return super().read_all()

    def write_frame(self, frame: JointFrame) -> None:
        self.call_threads.add(threading.get_ident())
        super().write_frame(frame)

    def stop(self) -> None:
        self.call_threads.add(threading.get_ident())
        super().stop()
        self.stop_event.set()

    def maintain_idle(self) -> None:
        self.call_threads.add(threading.get_ident())
        self.idle_maintain_count += 1
        super().maintain_idle()


class JogMotion:
    def __init__(self) -> None:
        self.cancel_reason: CancelReason | None = None
        self.steps = 0

    def request_cancel(self, reason: CancelReason) -> None:
        self.cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        del now
        self.steps += 1
        if self.cancel_reason is not None:
            backend.stop()
            return MotionStepResult.CANCELLED
        backend.write_frame(velocity_frame(0.2))
        return MotionStepResult.RUNNING


def test_all_backend_calls_are_marshaled_to_owner_thread() -> None:
    holder: dict[str, ThreadRecordingBackend] = {}

    def factory() -> ThreadRecordingBackend:
        backend = ThreadRecordingBackend()
        holder["backend"] = backend
        return backend

    loop = HardwareLoop(factory, control_hz=100.0)
    loop.start()
    try:
        caller_thread = threading.get_ident()
        result = loop.submit(lambda backend: len(backend.read_all())).result(timeout=1.0)
        assert result == 7
        assert loop.wait_for_cycles(3)
        assert holder["backend"].call_threads == {loop.thread_id}
        assert caller_thread not in holder["backend"].call_threads
    finally:
        loop.stop()


def test_client_cancel_is_forwarded_to_motion_state_machine() -> None:
    loop = HardwareLoop(SimBackend, control_hz=100.0)
    motion = JogMotion()
    loop.start()
    try:
        completion = loop.start_motion(motion)
        assert loop.wait_for_cycles(3)
        loop.request_cancel(CancelReason.CLIENT)
        assert completion.result(timeout=1.0) is MotionStepResult.CANCELLED
        assert motion.cancel_reason is CancelReason.CLIENT
        assert motion.steps >= 1
    finally:
        loop.stop()


def test_immediate_cancel_after_submission_is_not_lost() -> None:
    loop = HardwareLoop(SimBackend, control_hz=100.0)
    motion = JogMotion()
    loop.start()
    try:
        completion = loop.start_motion(motion)
        loop.request_cancel(CancelReason.CLIENT)
        assert completion.result(timeout=1.0) is MotionStepResult.CANCELLED
        assert motion.cancel_reason is CancelReason.CLIENT
    finally:
        loop.stop()


def test_estop_preempts_motion_and_latches_until_cleared() -> None:
    holder: dict[str, ThreadRecordingBackend] = {}

    def factory() -> ThreadRecordingBackend:
        backend = ThreadRecordingBackend()
        holder["backend"] = backend
        return backend

    loop = HardwareLoop(factory, control_hz=200.0)
    motion = JogMotion()
    loop.start()
    try:
        completion = loop.start_motion(motion)
        assert loop.wait_for_cycles(3)
        requested_at = time.monotonic()
        loop.request_estop()
        assert holder["backend"].stop_event.wait(0.1)
        assert time.monotonic() - requested_at < 0.1
        assert completion.result(timeout=1.0) is MotionStepResult.CANCELLED
        assert loop.estop_engaged

        blocked = loop.submit(lambda backend: backend.write_frame(velocity_frame(0.5)))
        time.sleep(0.03)
        assert not blocked.done()
        assert loop.clear_estop()
        blocked.result(timeout=1.0)
    finally:
        loop.stop()


def test_state_cache_and_cycle_stats_keep_advancing() -> None:
    loop = HardwareLoop(SimBackend, control_hz=100.0)
    loop.start()
    try:
        assert loop.wait_for_cycles(5)
        state = loop.latest_state()
        stats = loop.stats()
        assert state is not None
        assert len(state.motors) == 7
        assert state.age_s(time.monotonic()) < 0.1
        assert stats.cycles >= 5
        assert stats.actual_hz > 0
    finally:
        loop.stop()


def test_idle_cycles_repeat_zero_stiffness_damping_frame() -> None:
    holder: dict[str, ThreadRecordingBackend] = {}

    def factory() -> ThreadRecordingBackend:
        backend = ThreadRecordingBackend()
        holder["backend"] = backend
        return backend

    loop = HardwareLoop(factory, control_hz=100.0)
    loop.start()
    try:
        assert loop.wait_for_cycles(6)
        backend = holder["backend"]
        states = backend.read_all()
        assert backend.idle_maintain_count >= 5
        assert {state.mode for state in states} == {int(FrameMode.POS_VEL_TQE_KP_KD)}
        assert all(state.valid for state in states)
    finally:
        loop.stop()


def test_cycle_rate_excludes_slow_backend_initialization() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds
            time.sleep(0)

    clock = FakeClock()

    def factory() -> SimBackend:
        clock.advance(3.0)
        return SimBackend(clock=clock)

    loop = HardwareLoop(factory, control_hz=100.0, clock=clock, sleeper=clock.advance)
    loop.start()
    try:
        assert loop.wait_for_cycles(20)
        assert loop.stats().actual_hz == pytest.approx(100.0, rel=0.02)
    finally:
        loop.stop()
