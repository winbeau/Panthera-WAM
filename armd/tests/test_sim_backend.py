from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from armd.backend import (
    Backend,
    BackendClosedError,
    FrameMode,
    JointFrame,
    LimitViolationError,
    SimBackend,
)


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def pos_vel_frame(
    positions: list[float],
    velocities: list[float],
    *,
    gripper_position: float = 0.0,
    gripper_velocity: float = 0.5,
) -> JointFrame:
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE,
        arm_position=np.array(positions),
        arm_velocity=np.array(velocities),
        arm_max_torque=np.array([21.0, 36.0, 36.0, 21.0, 10.0, 10.0]),
        gripper_position=gripper_position,
        gripper_velocity=gripper_velocity,
    )


def velocity_frame(velocities: list[float]) -> JointFrame:
    return JointFrame(
        mode=FrameMode.VELOCITY,
        arm_position=np.zeros(6),
        arm_velocity=np.array(velocities),
        gripper_position=0.0,
        gripper_velocity=0.0,
    )


def mit_frame() -> JointFrame:
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE_KP_KD,
        arm_position=np.array([0.2, 0.2, 0.3, -0.2, 0.1, -0.3]),
        arm_velocity=np.zeros(6),
        arm_torque=np.array([100.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        arm_kp=np.array([30.0, 50.0, 60.0, 25.0, 15.0, 10.0]),
        arm_kd=np.array([3.0, 5.0, 6.0, 2.5, 1.5, 1.0]),
        gripper_position=0.4,
        gripper_velocity=0.0,
        gripper_torque=0.0,
        gripper_kp=5.0,
        gripper_kd=0.5,
    )


def test_initial_state_matches_backend_contract() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)

    assert isinstance(backend, Backend)
    states = backend.read_all()

    assert [state.name for state in states] == [f"joint{i}" for i in range(1, 8)]
    assert [state.motor_id for state in states] == list(range(1, 8))
    assert all(state.valid for state in states)
    assert all(state.position == 0.0 for state in states)
    assert all(state.mode == FrameMode.STOP for state in states)


def test_pos_vel_frame_moves_all_slots_without_overshoot() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend.write_frame(
        pos_vel_frame(
            [0.2, 0.2, 0.3, -0.2, 0.1, -0.3],
            [0.5] * 6,
            gripper_position=0.4,
            gripper_velocity=0.5,
        )
    )

    clock.advance(0.2)
    backend.refresh_state()
    first = backend.read_all()
    assert np.allclose([state.position for state in first], [0.1, 0.1, 0.1, -0.1, 0.1, -0.1, 0.1])
    assert all(state.mode == FrameMode.POS_VEL_TQE for state in first)

    clock.advance(1.0)
    backend.refresh_state()
    final = backend.read_all()
    assert np.allclose(
        [state.position for state in final],
        [0.2, 0.2, 0.3, -0.2, 0.1, -0.3, 0.4],
    )
    assert np.allclose([state.velocity for state in final], np.zeros(7))


def test_velocity_mode_clamps_at_soft_limit_and_sets_flag() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend.write_frame(velocity_frame([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]))

    clock.advance(3.0)
    backend.refresh_state()
    joint1 = backend.read_all()[0]

    assert joint1.position == pytest.approx(2.4)
    assert joint1.velocity == 0.0
    assert joint1.pos_limit_flag == 1


def test_mit_mode_advances_full_frame_and_clips_reported_torque() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend.write_frame(mit_frame())

    clock.advance(0.1)
    backend.refresh_state()
    states = backend.read_all()

    assert states[0].position > 0.0
    assert states[6].position > 0.0
    assert states[0].torque == pytest.approx(21.0)
    assert all(state.mode == FrameMode.POS_VEL_TQE_KP_KD for state in states)


def test_stop_freezes_motion_on_next_backend_cycle() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend.write_frame(velocity_frame([0.5, 0.0, 0.0, 0.0, 0.0, 0.0]))
    clock.advance(0.2)
    backend.stop()
    stopped_position = backend.read_all()[0].position

    clock.advance(5.0)
    backend.refresh_state()

    assert stopped_position == pytest.approx(0.1)
    assert backend.read_all()[0].position == pytest.approx(stopped_position)
    assert backend.read_all()[0].mode == FrameMode.STOP


def test_position_target_outside_limits_is_rejected_structurally() -> None:
    backend = SimBackend(clock=FakeClock())

    with pytest.raises(LimitViolationError, match=r"joint2.*下限 -0.1"):
        backend.write_frame(pos_vel_frame([0.0, -0.2, 0.0, 0.0, 0.0, 0.0], [0.5] * 6))


def test_set_zero_preserves_all_vs_selected_persistence_semantics() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend.write_frame(pos_vel_frame([0.1] * 6, [1.0] * 6, gripper_position=0.2, gripper_velocity=1.0))
    clock.advance(0.2)
    backend.refresh_state()

    accepted, persisted, reason = backend.set_zero()
    assert (accepted, persisted, reason) == (True, False, "")
    assert np.allclose([state.position for state in backend.read_all()], np.zeros(7))

    backend.write_frame(pos_vel_frame([0.1] * 6, [1.0] * 6, gripper_position=0.2, gripper_velocity=1.0))
    clock.advance(0.1)
    backend.refresh_state()
    accepted, persisted, reason = backend.set_zero([1, 7])
    states = backend.read_all()
    assert (accepted, persisted, reason) == (True, True, "")
    assert states[0].position == 0.0
    assert states[6].position == 0.0
    assert states[1].position == pytest.approx(0.1)


def test_disconnect_uses_sdk_sentinel_and_close_is_idempotent() -> None:
    backend = SimBackend(clock=FakeClock())
    backend.set_motor_connected(3, False)

    state = backend.read_all()[2]
    assert state.position == 999.0
    assert not state.valid

    backend.close()
    backend.close()
    with pytest.raises(BackendClosedError):
        backend.read_all()


def test_joint_frame_is_immutable_and_rejects_incomplete_mit_frame() -> None:
    source = np.zeros(6)
    frame = velocity_frame(source.tolist())
    source[0] = 1.0
    assert frame.arm_velocity[0] == 0.0
    assert not frame.arm_velocity.flags.writeable

    incomplete = JointFrame(
        mode=FrameMode.POS_VEL_TQE_KP_KD,
        arm_position=np.zeros(6),
        arm_velocity=np.zeros(6),
        gripper_position=0.0,
        gripper_velocity=0.0,
    )
    with pytest.raises(ValueError, match="arm_torque"):
        incomplete.validate()
