"""P3：WorkZeroMotion 运动状态机单测（spy backend，不碰真机）。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from armd.backend import FrameMode, SimBackend
from armd.hardware_loop import CancelReason, MotionStepResult
from armd.motion import (
    AutoHoldConfig,
    AutoHoldState,
    TeachClutchCommand,
    TeachMotion,
)
from armd.workzero import WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK, WorkZeroPose
from armd.workzero_motion import (
    WORKZERO_CANCEL_DECEL_STEPS,
    WORKZERO_KD,
    WORKZERO_KP,
    WORKZERO_SMALL_RESIDUAL,
    WorkZeroMotion,
    WorkZeroPhase,
)


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSimBackend(SimBackend):
    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.frames: list = []

    def write_frame(self, frame) -> None:
        self.frames.append(frame)
        super().write_frame(frame)


class IdealServoBackend(RecordingSimBackend):
    """理想 MIT 伺服：位置立即跟踪帧目标（模拟强刚度固件的收敛行为）。

    SimBackend 的 MIT 模型把 kp 当作速度增益（0.05·kp），与真实固件的力矩
    伺服动力学不可比；状态机逻辑（帧形态、fraction、取消、terminal 语义）
    用本后端做确定性验证，真实动力学收敛另由 P5 仿真集成覆盖。
    """

    def write_frame(self, frame) -> None:
        self.frames.append(frame)
        if frame.mode is FrameMode.POS_VEL_TQE_KP_KD:
            self._positions[:6] = np.clip(
                frame.arm_position, self._position_lower[:6], self._position_upper[:6]
            )
            self._velocities[:6] = frame.arm_velocity
            self._positions[6] = float(
                np.clip(frame.gripper_position, self.limits.gripper_lower, self.limits.gripper_upper)
            )
            self._velocities[6] = frame.gripper_velocity
        else:
            super().write_frame(frame)


class FrozenBackend(SimBackend):
    """位置永不变化的假后端：用于 settle 超时/不可达测试。"""

    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.frames: list = []
        self._frozen_positions = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)

    def write_frame(self, frame) -> None:
        self.frames.append(frame)

    def read_all(self):
        states = super().read_all()
        for index, state in enumerate(states[:6]):
            states[index] = state.__class__(
                name=state.name,
                motor_id=state.motor_id,
                position=self._frozen_positions[index],
                velocity=0.0,
                torque=0.0,
                motor_time=state.motor_time,
                mode=state.mode,
                fault=state.fault,
            )
        return states

    def refresh_state(self) -> None:
        return


class DisconnectedBackend(RecordingSimBackend):
    """返回无效电机快照的后端。"""

    def read_all(self):
        states = super().read_all()
        for index, state in enumerate(states):
            states[index] = state.__class__(
                name=state.name,
                motor_id=state.motor_id,
                position=999.0,
                velocity=0.0,
                torque=0.0,
                motor_time=state.motor_time,
                mode=state.mode,
                fault=state.fault,
            )
        return states


def pose(*, joints: tuple[float, ...] = (0.3, 0.4, 0.5, -0.2, 0.1, -0.1), gripper: float = 0.6) -> WorkZeroPose:
    return WorkZeroPose(
        schema_version=1,
        joints=joints,
        gripper=gripper,
        captured_at_ms=0,
        sampled_monotonic_ns=None,
        state_sequence=None,
        stream_instance_id="test",
        source=WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK,
    )


def run_until_terminal(
    motion: WorkZeroMotion,
    backend: RecordingSimBackend,
    clock: FakeClock,
    *,
    max_steps: int = 2000,
) -> MotionStepResult:
    for _ in range(max_steps):
        result = motion.step(backend, clock.now)
        if result is not MotionStepResult.RUNNING:
            return result
        clock.advance(0.005)
    raise AssertionError("WorkZeroMotion 未在限定步数内终止")


def test_constructor_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="joints"):
        WorkZeroMotion(target=pose(joints=(0.0, 0.0, 0.0)), timeout_s=10.0)
    with pytest.raises(ValueError, match="有限"):
        WorkZeroMotion(target=pose(joints=(np.nan, 0, 0, 0, 0, 0)), timeout_s=10.0)
    with pytest.raises(ValueError, match="timeout"):
        WorkZeroMotion(target=pose(), timeout_s=0.0)
    with pytest.raises(ValueError, match="timeout"):
        WorkZeroMotion(target=pose(), timeout_s=61.0)
    with pytest.raises(ValueError, match="kp/kd"):
        WorkZeroMotion(target=pose(), timeout_s=10.0, kp=np.zeros(6))
    with pytest.raises(ValueError, match="scale"):
        WorkZeroMotion(target=pose(), timeout_s=10.0, max_velocity_scale=1.5)


def test_motion_writes_only_mit_full_frames() -> None:
    clock = FakeClock()
    backend = IdealServoBackend(clock=clock)
    motion = WorkZeroMotion(target=pose(), timeout_s=30.0)
    result = run_until_terminal(motion, backend, clock)
    assert result is MotionStepResult.DONE
    assert len(backend.frames) > 5
    for frame in backend.frames:
        assert frame.mode is FrameMode.POS_VEL_TQE_KP_KD
        assert frame.arm_kp is not None and frame.arm_kd is not None
    # 禁止路径审计：绝不出现 POS_VEL_TQE 单帧目标
    assert all(frame.mode is FrameMode.POS_VEL_TQE_KP_KD for frame in backend.frames)


def test_signed_error_and_limits_respected() -> None:
    clock = FakeClock()
    backend = IdealServoBackend(clock=clock)
    target = pose(joints=(0.8, 0.9, 1.0, -0.5, -0.6, 0.7), gripper=1.2)
    motion = WorkZeroMotion(target=target, timeout_s=30.0)
    result = run_until_terminal(motion, backend, clock)
    assert result is MotionStepResult.DONE
    # signed error 方向与目标一致（final errors 收敛）
    assert motion.final_error is not None and motion.final_error <= motion.settle_tolerance
    # 轨迹中所有帧位置/速度不越限
    limits = backend.limits
    for frame in backend.frames:
        assert np.all(frame.arm_position >= limits.joint_lower - 1e-9)
        assert np.all(frame.arm_position <= limits.joint_upper + 1e-9)
        assert np.all(np.abs(frame.arm_velocity) <= limits.joint_velocity + 1e-9)
        assert limits.gripper_lower <= frame.gripper_position <= limits.gripper_upper
    assert motion.fraction == pytest.approx(1.0)
    assert motion.duration_s is not None and motion.duration_s > 0
    assert motion.start_pose is not None and len(motion.start_pose) == 6


def test_fraction_is_monotonic_and_timestamps_advance() -> None:
    clock = FakeClock()
    backend = IdealServoBackend(clock=clock)
    motion = WorkZeroMotion(target=pose(), timeout_s=30.0)
    previous = -1.0
    for _ in range(1000):
        result = motion.step(backend, clock.now)
        assert motion.fraction >= previous
        previous = motion.fraction
        if result is not MotionStepResult.RUNNING:
            assert result is MotionStepResult.DONE
            break
        clock.advance(0.005)
    else:
        raise AssertionError("未在限定步数内完成")
    assert motion.fraction == pytest.approx(1.0)


def test_small_residual_goes_straight_to_settle() -> None:
    clock = FakeClock()
    backend = IdealServoBackend(clock=clock)
    target = pose(joints=(0.005, -0.004, 0.003, 0.002, -0.001, 0.0005), gripper=0.0)
    motion = WorkZeroMotion(target=target, timeout_s=30.0)
    result = motion.step(backend, clock.now)
    # 小残差路径：跳过 PLAN/RUN 直接 SETTLE；理想伺服下可能当步即收敛
    assert result in (MotionStepResult.RUNNING, MotionStepResult.DONE)
    assert motion.phase in (WorkZeroPhase.SETTLE, WorkZeroPhase.DONE)
    assert motion.fraction == pytest.approx(1.0)
    result = run_until_terminal(motion, backend, clock, max_steps=2000)
    assert result is MotionStepResult.DONE
    # 所有帧仍是 MIT
    assert all(frame.mode is FrameMode.POS_VEL_TQE_KP_KD for frame in backend.frames)


def test_cancel_decelerates_then_cancelled_without_new_motion() -> None:
    clock = FakeClock()
    backend = IdealServoBackend(clock=clock)
    motion = WorkZeroMotion(target=pose(), timeout_s=30.0)
    # 进入 RUNNING
    for _ in range(30):
        assert motion.step(backend, clock.now) is MotionStepResult.RUNNING
        clock.advance(0.005)
    frames_before_cancel = len(backend.frames)
    motion.request_cancel(CancelReason.CLIENT)
    result = MotionStepResult.RUNNING
    for _ in range(WORKZERO_CANCEL_DECEL_STEPS + 2):
        result = motion.step(backend, clock.now)
        if result is not MotionStepResult.RUNNING:
            break
        clock.advance(0.005)
    assert result is MotionStepResult.CANCELLED
    decel_frames = backend.frames[frames_before_cancel:]
    assert 1 < len(decel_frames) <= WORKZERO_CANCEL_DECEL_STEPS + 1
    # 速度单调递减（目标速度按比例递减，末帧为零）
    speeds = [float(np.max(np.abs(frame.arm_velocity))) for frame in decel_frames]
    assert speeds[-1] == pytest.approx(0.0)
    assert all(speeds[i] >= speeds[i + 1] - 1e-12 for i in range(len(speeds) - 1))
    assert all(frame.mode is FrameMode.POS_VEL_TQE_KP_KD for frame in decel_frames)
    assert motion.terminal
    # terminal 后不再发帧
    frames = len(backend.frames)
    assert motion.step(backend, clock.now) is MotionStepResult.CANCELLED
    assert len(backend.frames) == frames


def test_plan_rejects_travel_beyond_timeout() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    # 大行程 + 极小 timeout：拒绝，不硬裁速度
    target = pose(joints=(2.0, 2.0, 2.0, 1.0, 1.0, 2.0), gripper=0.6)
    motion = WorkZeroMotion(target=target, timeout_s=0.5)
    result = run_until_terminal(motion, backend, clock, max_steps=10)
    assert result is MotionStepResult.FAILED
    assert "timeout" in motion.reject_reason
    # 失败收尾帧是 MIT hold，不是 position_frame
    assert all(frame.mode is FrameMode.POS_VEL_TQE_KP_KD for frame in backend.frames)


def test_settle_timeout_fails_with_safe_hold() -> None:
    clock = FakeClock()
    backend = FrozenBackend(clock=clock)
    motion = WorkZeroMotion(target=pose(), timeout_s=30.0, settle_timeout_s=0.2)
    result = run_until_terminal(motion, backend, clock, max_steps=2000)
    assert result is MotionStepResult.FAILED
    assert "收敛超时" in motion.reject_reason
    assert all(frame.mode is FrameMode.POS_VEL_TQE_KP_KD for frame in backend.frames)


def test_invalid_state_fails_immediately_without_dangerous_frame() -> None:
    clock = FakeClock()
    backend = DisconnectedBackend(clock=clock)
    motion = WorkZeroMotion(target=pose(), timeout_s=30.0)
    result = motion.step(backend, clock.now)
    assert result is MotionStepResult.FAILED
    assert "无效" in motion.reject_reason
    assert backend.frames == []


def test_gripper_target_out_of_limits_fails_at_validate() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = WorkZeroMotion(target=pose(gripper=2.5), timeout_s=30.0)
    result = run_until_terminal(motion, backend, clock, max_steps=10)
    assert result is MotionStepResult.FAILED
    assert "夹爪" in motion.reject_reason
    assert all(frame.mode is FrameMode.POS_VEL_TQE_KP_KD for frame in backend.frames)


def test_terminal_never_emits_frames_after_done() -> None:
    clock = FakeClock()
    backend = IdealServoBackend(clock=clock)
    motion = WorkZeroMotion(target=pose(), timeout_s=30.0)
    result = run_until_terminal(motion, backend, clock)
    assert result is MotionStepResult.DONE
    frames = len(backend.frames)
    for _ in range(5):
        assert motion.step(backend, clock.now) is MotionStepResult.DONE
    assert len(backend.frames) == frames


def test_gains_are_positive_and_fixed_by_server() -> None:
    # 客户端不能自定义增益：构造参数只接受服务端默认或显式保守值
    assert np.all(WORKZERO_KP > 0)
    assert np.all(WORKZERO_KD > 0)
    motion = WorkZeroMotion(target=pose(), timeout_s=10.0)
    assert np.array_equal(motion.kp, WORKZERO_KP)
    assert np.array_equal(motion.kd, WORKZERO_KD)


def test_operation_name_carries_rezero_metadata() -> None:
    motion = WorkZeroMotion(target=pose(), timeout_s=10.0, operation_name="rezero")
    assert motion.operation_name == "rezero"
    assert WORKZERO_SMALL_RESIDUAL > 0


# ------------------------------------------------- TeachMotion gripper 帧内伺服


def _teach_hold_motion(backend: RecordingSimBackend) -> TeachMotion:
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=AutoHoldConfig(),
        manual_clutch=True,
        safe_hold_time_s=2.0,
    )
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, backend._clock())
    return motion


def test_teach_request_gripper_servos_in_mit_frame_then_releases() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = _teach_hold_motion(backend)

    motion.request_gripper(1.5)
    clock.advance(0.005)
    motion.step(backend, clock.now)
    frame = backend.frames[-1]
    assert frame.mode is FrameMode.POS_VEL_TQE_KP_KD
    assert frame.gripper_position == pytest.approx(1.5)
    assert frame.gripper_kp == pytest.approx(5.0)
    assert frame.gripper_kd == pytest.approx(0.5)
    assert motion.gripper_target == pytest.approx(1.5)

    # 到位后恢复保持：gripper_kp/kd 归零，目标清除
    backend._positions[6] = 1.49
    clock.advance(0.005)
    motion.step(backend, clock.now)
    assert motion.gripper_target is None
    frame = backend.frames[-1]
    assert frame.gripper_kp == pytest.approx(0.0)
    assert frame.gripper_kd == pytest.approx(0.0)
    assert frame.gripper_position == pytest.approx(1.49)


def test_teach_request_gripper_clips_to_limits() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = _teach_hold_motion(backend)
    motion.request_gripper(5.0)  # 超出 gripper 上限 2.0
    clock.advance(0.005)
    motion.step(backend, clock.now)
    assert backend.frames[-1].gripper_position == pytest.approx(2.0)


def test_teach_request_gripper_rejects_nonfinite() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = _teach_hold_motion(backend)
    with pytest.raises(ValueError):
        motion.request_gripper(float("nan"))


def test_teach_cancel_quick_shortens_safe_hold() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = _teach_hold_motion(backend)
    motion.request_cancel_quick(CancelReason.CLIENT)
    clock.advance(0.005)
    result = motion.step(backend, clock.now)
    assert result is MotionStepResult.RUNNING
    assert motion.auto_hold_state is AutoHoldState.SAFE_HOLD
    # 快速退出：SAFE_HOLD 约 0.3s；完整退出为 safe_hold_time_s（2.0）
    assert motion._safe_hold_until is not None
    assert motion._safe_hold_until - clock.now == pytest.approx(0.3, abs=0.05)
    # 0.35s 后 SAFE_HOLD 结束进入柔顺阻尼并 CANCELLED
    clock.advance(0.35)
    result = motion.step(backend, clock.now)
    assert result is MotionStepResult.CANCELLED
