from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from armd.backend import FrameMode, IDLE_DAMPING_KD, SimBackend
from armd.hardware_loop import CancelReason, MotionStepResult
from armd.motion import (
    CartesianTrajectoryMotion,
    JOG_FRESHNESS_S,
    JointJogMotion,
    JointPositionMotion,
    gripper_position_frame,
)


@dataclass
class FakeClock:
    now: float = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSimBackend(SimBackend):
    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.frames = []

    def write_frame(self, frame) -> None:
        self.frames.append(frame)
        super().write_frame(frame)


def test_gripper_position_frame_uses_requested_instantaneous_torque_budget() -> None:
    backend = SimBackend()
    frame = gripper_position_frame(
        backend,
        arm_position=np.zeros(6),
        arm_filtered_velocity=np.array([0.2, -0.2, 0.1, -0.1, 0.05, -0.05]),
        gripper_position=0.05,
        gripper_current_position=-0.008,
        gripper_current_velocity=0.0,
        gripper_velocity=0.1,
        gripper_max_torque=0.1,
    )

    position_effort = frame.gripper_kp * abs(0.05 - (-0.008))
    velocity_effort = frame.gripper_kd * abs(frame.gripper_velocity - 0.0)
    assert position_effort + velocity_effort == pytest.approx(0.1)
    assert np.all(np.sign(frame.arm_torque) == [-1, 1, -1, 1, -1, 1])
    assert frame.arm_kp == pytest.approx([0.0] * 6)
    assert frame.arm_kd == pytest.approx([0.0] * 6)


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


def test_position_motion_sends_sdk_target_once_while_polling() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = JointPositionMotion(
        positions=np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        velocities=np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0]),
        max_torque=backend.limits.joint_torque,
        tolerance=1e-3,
        deadline=clock.now + 2.0,
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert len(backend.frames) == 1
    for _ in range(5):
        clock.advance(0.01)
        backend.refresh_state()
        assert motion.step(backend, clock.now) is MotionStepResult.RUNNING

    assert len(backend.frames) == 1


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
    frame = backend._last_frame
    assert frame is not None
    assert frame.mode is FrameMode.POS_VEL_TQE_KP_KD
    assert frame.arm_torque == pytest.approx([0.0] * 6)
    assert frame.arm_kp == pytest.approx([0.0] * 6)
    assert frame.arm_kd == pytest.approx(IDLE_DAMPING_KD)


def test_jog_blocks_velocity_toward_nearby_soft_limit() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    backend._positions[0] = backend.limits.joint_upper[0] - 0.01
    motion = JointJogMotion(clock=clock)
    motion.update(np.array([0.2, 0.0, 0.0, 0.0, 0.0, 0.0]))

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert motion.limit_hit[0]
    assert backend.read_all()[0].velocity == 0.0


def test_cartesian_cancel_uses_twelve_control_steps() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    motion = CartesianTrajectoryMotion(
        positions=[np.zeros(6), np.full(6, 0.1)],
        velocities=[np.full(6, 0.2), np.full(6, 0.2)],
        timestamps=[0.0, 1.0],
        max_torque=backend.limits.joint_torque,
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    motion.request_cancel(CancelReason.CLIENT)
    for _ in range(11):
        clock.advance(0.005)
        backend.refresh_state()
        assert motion.step(backend, clock.now) is MotionStepResult.RUNNING

    clock.advance(0.005)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.CANCELLED
    assert motion.reject_reason == "运动已取消: client"


def test_cartesian_small_target_is_not_done_inside_old_loose_tolerance() -> None:
    clock = FakeClock()
    backend = SimBackend(clock=clock)
    target = np.array([0.002, 0.0, 0.0, 0.0, 0.0, 0.0])
    motion = CartesianTrajectoryMotion(
        positions=[np.zeros(6), target],
        velocities=[np.zeros(6), np.zeros(6)],
        timestamps=[0.0, 1.0],
        max_torque=backend.limits.joint_torque,
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    backend._positions[:] = 0.0
    clock.advance(1.0)
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert motion.errors[0] == pytest.approx(0.002)


def test_cartesian_trajectory_preserves_signed_velocity_and_does_not_repeat_samples() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = CartesianTrajectoryMotion(
        positions=[
            np.zeros(6),
            np.array([-0.01, 0.0, 0.0, 0.0, 0.0, 0.0]),
            np.array([-0.02, 0.0, 0.0, 0.0, 0.0, 0.0]),
        ],
        velocities=[
            np.zeros(6),
            np.array([-0.5, 0.0, 0.0, 0.0, 0.0, 0.0]),
            np.zeros(6),
        ],
        timestamps=[0.0, 0.01, 0.02],
        max_torque=backend.limits.joint_torque,
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert len(backend.frames) == 1
    assert backend.frames[0].arm_position[0] == pytest.approx(0.0)

    clock.advance(0.005)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert len(backend.frames) == 1

    clock.advance(0.005)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert len(backend.frames) == 2
    assert backend.frames[-1].arm_position[0] == pytest.approx(-0.01)
    assert backend.frames[-1].arm_velocity[0] == pytest.approx(-0.5)


def test_cartesian_trajectory_finishes_with_zero_velocity_lock() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    target = np.array([0.02, 0.0, 0.0, 0.0, 0.0, 0.0])
    motion = CartesianTrajectoryMotion(
        positions=[np.zeros(6), target],
        velocities=[np.zeros(6), np.zeros(6)],
        timestamps=[0.0, 1.0],
        max_torque=backend.limits.joint_torque,
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    backend._positions[:6] = target
    clock.advance(1.0)

    assert motion.step(backend, clock.now) is MotionStepResult.DONE
    assert backend.frames[-1].arm_position == pytest.approx(target)
    assert backend.frames[-1].arm_velocity == pytest.approx([0.0] * 6)
