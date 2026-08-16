from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from armd.backend import FrameMode, SimBackend
from armd.hardware_loop import CancelReason, MotionStepResult
from armd.motion import (
    AutoHoldConfig,
    AutoHoldState,
    CartesianTrajectoryMotion,
    JOG_FRESHNESS_S,
    JOG_TARGET_LOOKAHEAD_S,
    MANUAL_CLUTCH_HOLD_RAMP_TIME_S,
    MANUAL_CLUTCH_KD_MIN_RATIO,
    MANUAL_CLUTCH_KP_HOLD,
    JointJogMotion,
    JointPositionMotion,
    PlaybackFrame,
    TeachClutchCommand,
    TeachMotion,
    TeachPlaybackMotion,
    gripper_position_frame,
    position_frame,
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


@pytest.mark.parametrize(
    ("measured_position", "commanded_position"),
    [
        (-0.008168, 0.0),
        (0.4, 0.4),
        (2.008168, 2.0),
    ],
)
def test_teach_clamps_measured_gripper_position_to_command_limits(
    measured_position: float,
    commanded_position: float,
) -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    backend._positions[6] = measured_position
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    frame = backend.frames[-1]
    assert frame.mode is FrameMode.POS_VEL_TQE_KP_KD
    assert frame.gripper_position == pytest.approx(commanded_position)
    assert frame.gripper_velocity == 0.0
    assert frame.gripper_torque == 0.0
    assert frame.gripper_kp == 0.0
    assert frame.gripper_kd == 0.0
    assert backend.read_all()[6].position == pytest.approx(measured_position)


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


def test_teach_playback_disables_only_gripper_velocity_limit() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    backend._positions[6] = 0.2
    motion = TeachPlaybackMotion(
        frames=[
            PlaybackFrame(
                timestamp_s=0.1,
                position=np.zeros(6),
                velocity=np.zeros(6),
                gripper_position=0.2,
                gripper_velocity=3.7,
            )
        ],
        mode="mit",
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        vel_threshold=0.0,
        tau_limit=np.ones(6),
        gripper_kp=5.0,
        gripper_kd=0.5,
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    clock.advance(0.01)
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING

    frame = backend.frames[-1]
    assert frame.gripper_velocity == pytest.approx(3.7)
    assert not frame.enforce_gripper_velocity_limit
    assert np.all(np.abs(frame.arm_velocity) <= backend.limits.joint_velocity)


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
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    clock.advance(0.1)
    backend.refresh_state()
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
    assert frame.arm_kd == pytest.approx([0.0] * 6)


def test_jog_loaded_joint_uses_acceleration_limited_position_frame() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = JointJogMotion(clock=clock)
    motion.update(np.array([0.0, 0.15, 0.0, 0.0, 0.0, 0.0]))

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    first = backend.frames[-1]
    assert first.mode is FrameMode.POS_VEL_TQE
    assert first.arm_position == pytest.approx([0.0] * 6)

    clock.advance(0.005)
    backend.refresh_state()
    current = np.array([state.position for state in backend.read_all()[:6]])
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    second = backend.frames[-1]

    applied_velocity = (second.arm_position[1] - current[1]) / JOG_TARGET_LOOKAHEAD_S
    assert applied_velocity == pytest.approx(0.01)
    assert second.arm_velocity[1] == pytest.approx(0.01)
    assert second.arm_velocity[[0, 2, 3, 4, 5]] == pytest.approx([0.1] * 5)
    assert second.arm_position[1] > current[1]
    assert second.arm_position[[0, 2, 3, 4, 5]] == pytest.approx(current[[0, 2, 3, 4, 5]])
    assert all(frame.mode is not FrameMode.VELOCITY for frame in backend.frames)


def test_jog_cancel_decelerates_before_entering_idle() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = JointJogMotion(clock=clock)
    motion.update(np.array([0.0, 0.2, 0.0, 0.0, 0.0, 0.0]))

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    clock.advance(0.1)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING

    motion.request_cancel(CancelReason.CLIENT)
    clock.advance(0.005)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    decelerating = backend.frames[-1]
    current = np.array([state.position for state in backend.read_all()[:6]])
    applied_velocity = (decelerating.arm_position[1] - current[1]) / JOG_TARGET_LOOKAHEAD_S
    assert applied_velocity == pytest.approx(0.16)

    clock.advance(0.02)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.CANCELLED
    assert backend.frames[-1].mode is FrameMode.POS_VEL_TQE_KP_KD


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


def test_teach_passes_gravity_scale_to_compensation() -> None:
    class ScaleRecordingBackend(SimBackend):
        def __init__(self) -> None:
            super().__init__()
            self.captured_scale: float | None = None

        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            self.captured_scale = gravity_scale
            return np.zeros(6)

    backend = ScaleRecordingBackend()
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        gravity_scale=0.7,
    )
    assert motion.step(backend, 0.0) is MotionStepResult.RUNNING
    assert backend.captured_scale == pytest.approx(0.7)


def test_teach_rejects_invalid_gravity_scale() -> None:
    kwargs = {"kp": np.zeros(6), "kd": np.zeros(6), "fc": np.zeros(6), "fv": np.zeros(6)}
    with pytest.raises(ValueError):
        TeachMotion(**kwargs, gravity_scale=0.0)
    with pytest.raises(ValueError):
        TeachMotion(**kwargs, gravity_scale=-1.0)
    with pytest.raises(ValueError):
        TeachMotion(**kwargs, gravity_scale=float("nan"))


def test_teach_passes_segmented_gravity_scale_to_compensation() -> None:
    class SegmentedBackend(SimBackend):
        def __init__(self) -> None:
            super().__init__()
            self.captured: tuple | None = None

        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            self.captured = (gravity_scale, gravity_scale_high, gravity_breakpoint)
            return np.zeros(6)

    backend = SegmentedBackend()
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        gravity_scale=0.85,
        gravity_scale_high=np.array([1.7] * 6),
        gravity_breakpoint=np.array([1.2, np.inf, np.inf, np.inf, np.inf, np.inf]),
        gravity_segmented=True,
    )
    assert motion.step(backend, 0.0) is MotionStepResult.RUNNING
    scale, high, breakpoint = backend.captured
    assert scale == pytest.approx(0.85)
    assert np.all(high == pytest.approx(1.7))
    assert breakpoint[0] == pytest.approx(1.2)
    assert np.isinf(breakpoint[1])


def test_teach_rejects_invalid_segmented_gravity_scale() -> None:
    kwargs = {"kp": np.zeros(6), "kd": np.zeros(6), "fc": np.zeros(6), "fv": np.zeros(6)}
    with pytest.raises(ValueError):
        TeachMotion(**kwargs, gravity_scale_high=np.zeros(6))
    with pytest.raises(ValueError):
        TeachMotion(**kwargs, gravity_breakpoint=np.array([-1.0] * 6))


def test_teach_disables_segmented_gravity_by_default() -> None:
    class RecordingBackend(SimBackend):
        def __init__(self) -> None:
            super().__init__()
            self.captured: tuple | None = None

        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            self.captured = (gravity_scale, gravity_scale_high, gravity_breakpoint)
            return np.zeros(6)

    backend = RecordingBackend()
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        gravity_scale=0.78,
        gravity_scale_high=np.full(6, 1.9),
        gravity_breakpoint=np.array([1.2, np.inf, np.inf, np.inf, np.inf, np.inf]),
    )
    assert motion.step(backend, 0.0) is MotionStepResult.RUNNING
    assert backend.captured is not None
    assert backend.captured[1] is None
    assert backend.captured[2] is None


def test_teach_adds_continuous_gravity_residual_before_limit() -> None:
    class ResidualRecordingBackend(SimBackend):
        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            return np.full(6, 0.25)

    backend = ResidualRecordingBackend()
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        gravity_residual=np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0]),
    )
    assert motion.step(backend, 0.0) is MotionStepResult.RUNNING
    assert backend._last_frame is not None
    assert backend._last_frame.arm_torque[0] == pytest.approx(0.35)


def test_teach_residual_is_continuous_across_zero_crossing() -> None:
    class ContinuousGravityBackend(RecordingSimBackend):
        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            # 连续的近似过轴曲线；测试不允许出现断点式跳变。
            return np.array([0.0, 0.6 - float(q[1]), 0.0, 0.0, 0.0, 0.0])

    clock = FakeClock()
    backend = ContinuousGravityBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        gravity_residual=np.array([0.0, 0.1, 0.0, 0.0, 0.0, 0.0]),
    )
    torques = []
    for q2 in (1.199, 1.201):
        backend._positions[1] = q2
        clock.advance(0.005)
        backend.refresh_state()
        motion.step(backend, clock.now)
        torques.append(float(backend.frames[-1].arm_torque[1]))
    assert abs(torques[1] - torques[0]) == pytest.approx(0.002, abs=1e-4)
    assert all(np.isfinite(torques))


def _drag_until_still_holds(clock, backend, motion, *, drag_steps: int = 5) -> None:
    """模拟拖动 drag_steps 步后松手，推进 0.21s 进入 HOLD。

    注意 SimBackend 的 MIT 积分会在 refresh_state/write_frame 时改写速度，
    因此「拖动」必须在 refresh_state 之后、motion.step 之前设置反馈速度。
    """
    for _ in range(drag_steps):
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.1
        assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    for _ in range(60):  # 60 x 5ms = 0.3s：~10 步滤波衰减 + 0.2s 静止窗口
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.0
        assert motion.step(backend, clock.now) is MotionStepResult.RUNNING


def test_teach_shadow_step_advances_hold_without_writing_frames() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        manual_clutch=True,
        initial_hold=True,
    )
    clock.advance(0.005)
    assert motion.step(backend, clock.now, shadow=True) is MotionStepResult.RUNNING
    assert motion.auto_hold_state is AutoHoldState.HOLD
    assert motion.hold_position == pytest.approx([0.0] * 6)
    assert backend.frames == []


def test_manual_clutch_initial_hold_then_explicit_lock_samples_current_position() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    cfg = AutoHoldConfig(hold_ramp_time=0.4)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=cfg,
        manual_clutch=True,
        initial_hold=True,
    )

    clock.advance(0.005)
    backend.refresh_state()
    backend._velocities[:6] = 0.0
    motion.step(backend, clock.now)
    assert motion.auto_hold_state is AutoHoldState.HOLD
    assert backend.frames[-1].arm_kp == pytest.approx([0.0] * 6)

    for _ in range(59):
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.0
        motion.step(backend, clock.now)
    assert motion.auto_hold_state is AutoHoldState.HOLD
    assert motion.hold_position == pytest.approx([0.0] * 6)

    backend._positions[:6] = np.array([0.1, 1.3, 0.2, -0.1, 0.05, -0.05])
    backend._velocities[:6] = 0.1
    expected = backend._positions[:6].copy()
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, clock.now)

    assert motion.auto_hold_state is AutoHoldState.HOLD
    assert motion.hold_position == pytest.approx(expected)
    assert backend.frames[-1].arm_position == pytest.approx(expected)
    assert backend.frames[-1].arm_kp == pytest.approx(MANUAL_CLUTCH_KP_HOLD)

    for _ in range(16):
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.1
        motion.step(backend, clock.now)
    assert motion.auto_hold_state is AutoHoldState.HOLD
    assert backend.frames[-1].arm_kp == pytest.approx(MANUAL_CLUTCH_KP_HOLD, abs=0.05)
    assert backend.frames[-1].arm_kd == pytest.approx(
        np.maximum.reduce(
            [np.asarray(cfg.kd_hold), motion.kd, MANUAL_CLUTCH_KP_HOLD * MANUAL_CLUTCH_KD_MIN_RATIO]
        )
    )
    assert 16 * 0.005 == pytest.approx(MANUAL_CLUTCH_HOLD_RAMP_TIME_S)


def test_manual_clutch_initial_hold_yields_to_explicit_drag() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        manual_clutch=True,
        initial_hold=True,
    )
    motion.request_clutch(TeachClutchCommand.DRAG)
    motion.step(backend, clock.now)

    assert motion.auto_hold_state is AutoHoldState.DRAG
    assert motion.hold_position is None
    assert backend.frames[-1].arm_kp == pytest.approx([0.0] * 6)


def test_manual_clutch_hold_anchors_compensation_at_lock_pose_and_zero_velocity() -> None:
    class CapturingBackend(RecordingSimBackend):
        def __init__(self, *, clock: FakeClock) -> None:
            super().__init__(clock=clock)
            self.compensation_inputs = []

        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            self.compensation_inputs.append((np.asarray(q).copy(), np.asarray(v).copy()))
            return np.asarray(q) + np.asarray(v)

    clock = FakeClock()
    backend = CapturingBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.array([0.4, 2.0, 0.6, 0.4, 0.15, 0.08]),
        fc=np.full(6, 0.15),
        fv=np.full(6, 0.06),
        manual_clutch=True,
    )
    hold = np.array([0.1, 1.3, 0.2, -0.1, 0.05, -0.05])
    backend._positions[:6] = hold
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, clock.now)

    backend._positions[:6] = hold + 0.2
    backend._velocities[:6] = 0.1
    clock.advance(0.005)
    motion.step(backend, clock.now)

    q_used, v_used = backend.compensation_inputs[-1]
    assert q_used == pytest.approx(hold)
    assert v_used == pytest.approx([0.0] * 6)
    assert backend.frames[-1].arm_torque == pytest.approx(hold)


def test_manual_clutch_drag_releases_smoothly_then_stays_drag() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    cfg = AutoHoldConfig(hold_ramp_time=0.05, release_ramp_time=0.2)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=cfg,
        manual_clutch=True,
    )
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, clock.now)
    for _ in range(12):
        clock.advance(0.005)
        backend.refresh_state()
        motion.step(backend, clock.now)
    assert motion.hold_kp.max() > 0.0

    motion.request_clutch(TeachClutchCommand.DRAG)
    motion.step(backend, clock.now)
    assert motion.auto_hold_state is AutoHoldState.RELEASE
    previous = backend.frames[-1].arm_kp.copy()
    for _ in range(44):
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.0
        motion.step(backend, clock.now)
        current = backend.frames[-1].arm_kp
        assert np.all(current <= previous + 1e-9)
        previous = current

    assert motion.auto_hold_state is AutoHoldState.DRAG
    assert motion.hold_position is None
    assert backend.frames[-1].arm_kp == pytest.approx([0.0] * 6)
    for _ in range(60):
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.0
        motion.step(backend, clock.now)
    assert motion.auto_hold_state is AutoHoldState.DRAG


def test_manual_clutch_rejects_command_when_not_enabled() -> None:
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
    )
    with pytest.raises(ValueError, match="未启用显式 clutch"):
        motion.request_clutch(TeachClutchCommand.LOCK)


def test_manual_clutch_cancel_enters_safe_hold_then_limps() -> None:
    class CapturingBackend(RecordingSimBackend):
        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            return np.asarray(q) + np.asarray(v)

    clock = FakeClock()
    backend = CapturingBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.array([0.4, 2.0, 0.6, 0.4, 0.15, 0.08]),
        fc=np.full(6, 0.15),
        fv=np.full(6, 0.06),
        manual_clutch=True,
        safe_hold_time_s=0.2,
    )
    pose = np.array([0.1, 1.3, 0.2, -0.1, 0.05, -0.05])
    backend._positions[:6] = pose
    for _ in range(3):
        clock.advance(0.005)
        backend.refresh_state()
        motion.step(backend, clock.now)

    motion.request_cancel(CancelReason.WATCHDOG)
    clock.advance(0.005)
    backend.refresh_state()
    backend._positions[:6] = pose  # 模拟保持期间的轻微外部扰动
    result = motion.step(backend, clock.now)

    assert result is MotionStepResult.RUNNING
    assert motion.auto_hold_state is AutoHoldState.SAFE_HOLD
    assert motion.safe_holding
    assert motion.hold_position == pytest.approx(pose)
    assert backend.frames[-1].arm_position == pytest.approx(pose)
    # SAFE_HOLD 帧：锚定重力/摩擦前馈（q_hold、零速度）+ 强位置刚度
    assert backend.frames[-1].arm_torque == pytest.approx(pose)

    for _ in range(20):
        clock.advance(0.005)
        backend.refresh_state()
        motion.step(backend, clock.now)
    assert backend.frames[-1].arm_kp == pytest.approx(MANUAL_CLUTCH_KP_HOLD, abs=0.05)

    for _ in range(32):
        clock.advance(0.005)
        backend.refresh_state()
    result = motion.step(backend, clock.now)
    assert result is MotionStepResult.CANCELLED
    # SAFE_HOLD 退出末帧：重力前馈 + 满刚度锚定（不写 idle_damping 软帧）
    assert backend.frames[-1].mode is FrameMode.POS_VEL_TQE_KP_KD
    assert backend.frames[-1].arm_kp == pytest.approx(MANUAL_CLUTCH_KP_HOLD, abs=0.05)
    assert backend.frames[-1].arm_torque == pytest.approx(pose)


def test_safe_hold_from_hold_preserves_kp_on_first_frame() -> None:
    # Fix A 回归：从 HOLD 取消进 SAFE_HOLD，首帧保持满刚度，不从 0 崩塌
    # （kp 60→0 会让承重轴下坠再欠阻尼抓回，真机大幅震颤根因）。
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.array([0.4, 2.0, 0.6, 0.4, 0.15, 0.08]),
        fc=np.full(6, 0.15),
        fv=np.full(6, 0.06),
        manual_clutch=True,
        safe_hold_time_s=0.2,
    )
    pose = np.array([0.1, 1.3, 0.2, -0.1, 0.05, -0.05])
    backend._positions[:6] = pose
    motion.request_clutch(TeachClutchCommand.LOCK)
    for _ in range(30):  # 0.15s > 0.08s ramp：kp 爬满 HOLD
        clock.advance(0.005)
        backend.refresh_state()
        motion.step(backend, clock.now)
    assert motion.auto_hold_state is AutoHoldState.HOLD

    motion.request_cancel(CancelReason.CLIENT)
    clock.advance(0.005)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert motion.auto_hold_state is AutoHoldState.SAFE_HOLD
    assert backend.frames[-1].arm_kp == pytest.approx(MANUAL_CLUTCH_KP_HOLD, abs=0.05)


def test_teach_playback_move_to_start_writes_beyond_limit_raw() -> None:
    # 执行回放自由臂位（与手拖录制一致）：轨迹越限帧原样下发，不被软限位
    # 截断（真机 023 曾因 clip 导致末点无法收敛、settle 假超时 FAILED）。
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    first = np.array([0.0, 0.0, 0.0, -1.6041, 0.0, 0.0])
    current = np.array([0.0, 0.0, 0.0, -1.0, 0.0, 0.0])
    backend._positions[:6] = current
    backend._positions[6] = 1.79
    motion = _make_posvel_playback(
        [
            PlaybackFrame(
                timestamp_s=0.0,
                position=first,
                velocity=np.zeros(6),
                gripper_position=1.79,
                gripper_velocity=0.0,
            ),
            PlaybackFrame(
                timestamp_s=0.01,
                position=first,
                velocity=np.zeros(6),
                gripper_position=1.79,
                gripper_velocity=0.0,
            ),
        ]
    )
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert backend.frames[-1].arm_position[3] == pytest.approx(-1.6041)


def test_position_frame_clips_arm_position_by_default() -> None:
    # 非回放路径（定死锁/移动命令等）保持软限位 clip
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    frame = position_frame(
        backend,
        arm_position=np.array([0.0, 0.0, 0.0, -1.6041, 0.0, 0.0]),
        arm_velocity=np.zeros(6),
        gripper_position=1.79,
    )
    assert frame.arm_position[3] == pytest.approx(backend.limits.joint_lower[3])
    assert frame.enforce_arm_position_limit is True


def test_manual_clutch_safe_hold_rejects_new_clutch_commands() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        manual_clutch=True,
        safe_hold_time_s=0.2,
    )
    motion.request_cancel(CancelReason.CLIENT)
    motion.step(backend, clock.now)
    assert motion.safe_holding

    motion.request_clutch(TeachClutchCommand.DRAG)
    clock.advance(0.005)
    backend.refresh_state()
    motion.step(backend, clock.now)
    # SAFE_HOLD 期间忽略离合命令
    assert motion.auto_hold_state is AutoHoldState.SAFE_HOLD


def test_manual_clutch_hold_kd_never_below_drag_or_kp_ratio() -> None:
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.array([0.4, 2.0, 0.6, 0.4, 0.15, 0.08]),
        fc=np.zeros(6),
        fv=np.zeros(6),
        manual_clutch=True,
    )
    expected = np.maximum.reduce(
        [
            np.asarray(motion.auto_hold_cfg.kd_hold),
            motion.kd,
            MANUAL_CLUTCH_KP_HOLD * MANUAL_CLUTCH_KD_MIN_RATIO,
        ]
    )
    assert motion._manual_kd_hold == pytest.approx(expected)
    # J2/J3 承重轴：kp=20 时 kd 至少 1.6
    assert motion._manual_kd_hold[1] >= 1.6
    assert motion._manual_kd_hold[2] >= 1.6


def test_auto_hold_locks_position_after_still() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=AutoHoldConfig(),
    )
    assert motion.auto_hold_state is AutoHoldState.DRAG
    _drag_until_still_holds(clock, backend, motion)

    assert motion.auto_hold_state is AutoHoldState.HOLD
    assert motion.hold_position is not None
    assert np.allclose(motion.hold_position, backend._positions[:6])
    # HOLD 帧：位置指令 = 锁定位
    frame = backend.frames[-1]
    assert np.allclose(frame.arm_position, motion.hold_position)


def test_auto_hold_kp_ramps_smoothly_to_kp_hold() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    cfg = AutoHoldConfig(hold_ramp_time=0.4)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=cfg,
    )
    _drag_until_still_holds(clock, backend, motion)
    assert motion.auto_hold_state is AutoHoldState.HOLD

    previous = None
    for _ in range(80):  # 80 x 5ms = 0.4s
        clock.advance(0.005)
        backend.refresh_state()
        motion.step(backend, clock.now)
        kp = backend.frames[-1].arm_kp
        assert np.all(kp >= 0.0)
        if previous is not None:
            # 单调非降（smoothstep），无瞬间跳变
            assert np.all(kp >= previous - 1e-9)
        previous = kp
    assert np.allclose(backend.frames[-1].arm_kp, cfg.kp_hold, atol=0.05)


def test_auto_hold_release_on_redrag_and_returns_to_drag() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    cfg = AutoHoldConfig(release_ramp_time=0.2)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=cfg,
    )
    _drag_until_still_holds(clock, backend, motion)
    assert motion.auto_hold_state is AutoHoldState.HOLD

    # 重新拖动：速度 > release_velocity_threshold（低通滤波需几步爬升）
    for _ in range(10):
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.1
        motion.step(backend, clock.now)
        if motion.auto_hold_state is AutoHoldState.RELEASE:
            break
    assert motion.auto_hold_state is AutoHoldState.RELEASE

    kp_before = motion.hold_kp.copy()
    previous = None
    for _ in range(44):  # 0.22s > release_ramp_time
        clock.advance(0.005)
        backend.refresh_state()
        backend._velocities[:6] = 0.1
        motion.step(backend, clock.now)
        kp = backend.frames[-1].arm_kp
        if previous is not None:
            assert np.all(kp <= previous + 1e-9)  # 平滑单调降
        previous = kp
    assert motion.auto_hold_state is AutoHoldState.DRAG
    assert np.allclose(backend.frames[-1].arm_kp, 0.0, atol=1e-6)
    assert kp_before.max() > 0.0


def test_auto_hold_hold_does_not_depend_on_zero_gravity_residual() -> None:
    class ResidualBackend(RecordingSimBackend):
        def compensation_torque(
            self,
            q,
            v,
            fc,
            fv,
            vel_threshold,
            gravity_scale=1.0,
            gravity_scale_high=None,
            gravity_breakpoint=None,
        ):
            # J1 存在固定 0.1 Nm 的重力残差（模型不精确）
            return np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0])

    clock = FakeClock()
    backend = ResidualBackend(clock=clock)
    cfg = AutoHoldConfig(kp_hold=(1.0, 2.0, 2.0, 1.0, 0.8, 0.8))
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=cfg,
    )
    _drag_until_still_holds(clock, backend, motion)
    assert motion.auto_hold_state is AutoHoldState.HOLD
    hold = motion.hold_position

    # HOLD 帧：重力残差仍在前馈中，kp 提供位置刚度抵消残差
    frame = backend.frames[-1]
    assert frame.arm_torque[0] == pytest.approx(0.1)
    assert frame.arm_kp[0] > 0.0

    # 推进 0.6s：kp 渐变完成 + Sim 积分收敛，位置保持在小偏移内
    for _ in range(120):
        clock.advance(0.005)
        backend.refresh_state()
        motion.step(backend, clock.now)
    assert motion.auto_hold_state is AutoHoldState.HOLD
    drift = np.abs(backend._positions[:6] - hold)
    # 残差 0.1 Nm 由 kp=1.0 抵消，稳态偏移约 0.02 rad（Sim MIT 增益 0.05）
    assert np.all(drift < 0.05)


def _make_posvel_playback(frames) -> TeachPlaybackMotion:
    return TeachPlaybackMotion(
        frames=frames,
        mode="posvel",
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        vel_threshold=0.0,
        tau_limit=np.ones(6),
        gripper_kp=5.0,
        gripper_kd=0.5,
    )


def test_teach_playback_move_to_start_engages_small_offset() -> None:
    # 真机震颤根因回归：轨迹首帧与当前位形只差 ~0.0108 rad（J2）时，
    # 旧的 0.05 容差会跳过起点移动，回放第一帧就是位置阶跃。
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    first = np.array([-0.666, 1.0028, 1.026, -1.006, 0.034, -0.009])
    current = np.array([-0.666, 0.992, 1.026, -1.006, 0.034, -0.009])
    backend._positions[:6] = current
    backend._positions[6] = 1.79
    motion = _make_posvel_playback(
        [
            PlaybackFrame(
                timestamp_s=0.0,
                position=first,
                velocity=np.zeros(6),
                gripper_position=1.79,
                gripper_velocity=0.0,
            ),
            PlaybackFrame(
                timestamp_s=0.01,
                position=first,
                velocity=np.zeros(6),
                gripper_position=1.79,
                gripper_velocity=0.0,
            ),
        ]
    )

    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    # 0.0108 > 0.003：起点移动阶段必须生效，不能直接进入回放
    assert motion._playback_started_at is None
    assert backend.frames[-1].arm_position[1] == pytest.approx(first[1])

    # 到位后进入回放（起点移动只写目标，不跳变）
    backend._positions[:6] = first
    clock.advance(0.01)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert motion._playback_started_at is not None


def test_teach_playback_posvel_interpolates_and_preserves_signed_velocity() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    start = np.zeros(6)
    target = np.array([-0.01, 0.0, 0.0, 0.0, 0.0, 0.0])
    backend._positions[:6] = start
    backend._positions[6] = 1.79
    motion = _make_posvel_playback(
        [
            PlaybackFrame(
                timestamp_s=0.0,
                position=start,
                velocity=np.array([-0.5, 0.0, 0.0, 0.0, 0.0, 0.0]),
                gripper_position=1.79,
                gripper_velocity=-0.7,
            ),
            PlaybackFrame(
                timestamp_s=0.01,
                position=target,
                velocity=np.zeros(6),
                gripper_position=1.79,
                gripper_velocity=0.0,
            ),
        ]
    )

    # 首步：起点已到位 → 直接进入回放（不写帧）
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert backend.frames == []
    # elapsed=0：alpha=0 → 起点位形 + 符号速度（臂速度不得 abs / 不得 1e-3 下限）；
    # 夹爪速度按真机后端约束取非负幅值 + 1e-3 下限（真机曾因此 ValueError 崩溃）
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert backend.frames[0].arm_position[0] == pytest.approx(start[0])
    assert backend.frames[0].arm_velocity[0] == pytest.approx(-0.5)
    assert backend.frames[0].gripper_velocity == pytest.approx(0.7)

    # 中点：相邻帧插值（目标小步推进，而非采样点阶跃）
    clock.advance(0.005)
    backend.refresh_state()
    assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
    assert backend.frames[-1].arm_position[0] == pytest.approx(-0.005)
    assert backend.frames[-1].arm_velocity[0] == pytest.approx(-0.25)
    assert backend.frames[-1].gripper_velocity == pytest.approx(0.35)
