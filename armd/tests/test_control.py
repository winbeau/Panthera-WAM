from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from armd.backend import FrameMode, JointFrame, SimBackend
from armd.control import LeaseManager
from armd.safety import WatchdogStopAction, apply_watchdog_stop


@dataclass
class FakeClock:
    now: float = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def velocity_frame(value: float) -> JointFrame:
    return JointFrame(
        mode=FrameMode.VELOCITY,
        arm_position=np.zeros(6),
        arm_velocity=np.array([value, 0.0, 0.0, 0.0, 0.0, 0.0]),
        gripper_position=0.0,
        gripper_velocity=0.0,
    )


def position_frame(target: float) -> JointFrame:
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE,
        arm_position=np.array([target, 0.0, 0.0, 0.0, 0.0, 0.0]),
        arm_velocity=np.full(6, 0.5),
        arm_max_torque=np.array([21.0, 36.0, 36.0, 21.0, 10.0, 10.0]),
        gripper_position=0.0,
        gripper_velocity=0.5,
    )


def test_lease_contention_force_and_expiration() -> None:
    clock = FakeClock()
    leases = LeaseManager(timeout_s=1.0, clock=clock)

    first = leases.acquire("cli-a")
    denied = leases.acquire("cli-b")
    assert first.granted
    assert not denied.granted
    assert denied.holder_client_id == "cli-a"

    forced = leases.acquire("cli-b", force=True)
    assert forced.granted and forced.replaced_holder
    assert not leases.validate(first.token)
    assert leases.validate(forced.token)

    clock.advance(1.1)
    assert not leases.validate(forced.token)
    assert not leases.release(forced.token)
    expired = leases.expire_if_stale()
    assert expired is not None
    assert expired.holder_client_id == "cli-b"
    assert not leases.snapshot().held


def test_watchdog_enters_damping_from_position_mode() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend.write_frame(position_frame(0.5))
    clock.advance(0.2)
    backend.refresh_state()
    before = backend.read_all()[0].position

    action = apply_watchdog_stop(backend)
    clock.advance(1.0)
    backend.refresh_state()
    after = backend.read_all()[0].position

    assert action is WatchdogStopAction.IDLE_DAMPING
    assert after == before
    assert backend.read_all()[0].mode == FrameMode.POS_VEL_TQE_KP_KD


def test_watchdog_enters_damping_from_velocity_mode() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend.write_frame(velocity_frame(0.5))
    clock.advance(0.2)
    backend.refresh_state()

    action = apply_watchdog_stop(backend)
    stopped_at = backend.read_all()[0].position
    clock.advance(1.0)
    backend.refresh_state()

    assert action is WatchdogStopAction.IDLE_DAMPING
    assert backend.read_all()[0].position == stopped_at
    assert backend.read_all()[0].velocity == 0.0
    assert backend.read_all()[0].mode == FrameMode.POS_VEL_TQE_KP_KD
