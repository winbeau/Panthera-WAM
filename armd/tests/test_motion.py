from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from armd.backend import SimBackend
from armd.hardware_loop import CancelReason, MotionStepResult
from armd.motion import JOG_FRESHNESS_S, JointJogMotion, JointPositionMotion


@dataclass
class FakeClock:
    now: float = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_position_motion_reaches_and_holds_target() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    motion = JointPositionMotion(
        positions=np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0]),
        velocities=np.full(6, 0.5),
        max_torque=backend.limits.joint_torque,
        tolerance=1e-3,
        deadline=clock.now + 1.0,
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    for _ in range(30):
        clock.advance(0.01)
        backend.refresh_state()
        result = motion.step(backend, clock.now)
        if result is MotionStepResult.DONE:
            break

    assert result is MotionStepResult.DONE
    assert np.isclose(backend.read_all()[0].position, 0.1)
    assert motion.errors[0] <= 1e-3


def test_position_motion_timeout_holds_current_position() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    motion = JointPositionMotion(
        positions=np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        velocities=np.full(6, 0.1),
        max_torque=backend.limits.joint_torque,
        tolerance=1e-3,
        deadline=clock.now + 0.05,
    )
    motion.step(backend, clock.now)
    clock.advance(0.06)
    backend.refresh_state()

    assert motion.step(backend, clock.now) is MotionStepResult.FAILED
    stopped = backend.read_all()[0].position
    clock.advance(1.0)
    backend.refresh_state()
    assert backend.read_all()[0].position == stopped
    assert motion.reject_reason == "等待关节到位超时"


def test_jog_stale_window_zeroes_velocity_and_cancel_finishes() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    motion = JointJogMotion(clock=clock)
    motion.update(np.array([0.2, 0.0, 0.0, 0.0, 0.0, 0.0]))

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    clock.advance(0.1)
    backend.refresh_state()
    moving_position = backend.read_all()[0].position
    assert moving_position > 0.0

    clock.advance(JOG_FRESHNESS_S + 0.01)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    stopped_position = backend.read_all()[0].position
    clock.advance(0.2)
    backend.refresh_state()
    assert backend.read_all()[0].position == stopped_position

    motion.request_cancel(CancelReason.CLIENT)
    assert motion.step(backend, clock.now) is MotionStepResult.CANCELLED


def test_jog_blocks_velocity_toward_nearby_soft_limit() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend._positions[0] = backend.limits.joint_upper[0] - 0.01
    motion = JointJogMotion(clock=clock)
    motion.update(np.array([0.2, 0.0, 0.0, 0.0, 0.0, 0.0]))

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert motion.limit_hit[0]
    assert backend.read_all()[0].velocity == 0.0
