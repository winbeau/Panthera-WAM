"""P3：服务端连续流式 WorkZeroMotion（work-zero 方案 WZ-2）。

第一版只做关节空间：目标由 `WorkZeroPose` 定义，轨迹、软限位、速度/加速度、
力矩、取消与 EStop 交互全部在服务端 HardwareLoop 内完成，客户端只提交目标和
观察 execution。

安全契约（docs/FINAL_PLAN.md WZ-2 / docs/JOINT_CONTROL.md §7）：
- 每个控制周期发送**完整** `POS_VEL_TQE_KP_KD`（MIT）帧，7 槽同模式；
- 严禁调用 `position_frame()`、`JointPositionMotion`、MoveJ/moveL、
  TeachPlayback posvel 起点移动、RunJointTrajectory/CartesianTrajectoryMotion；
- 目标、当前状态、软限位、超时、增益在服务端二次校验；
- 小残差（低于冻结阈值）直接进入已验证的 MIT settle，不偷退回单帧位置模式；
- terminal 后 `step()` 不再发送任何帧。

默认增益/时长均为保守值，且真机侧由 `PANTHERA_WORKZERO_REAL_HARDWARE_ENABLED`
feature gate 关闭，只有 P5 软件 gate 与 P6 分级验收通过后才允许放行。
"""

from __future__ import annotations

import enum
import logging
import threading

import numpy as np

from .backend import Backend, FrameMode, JointFrame
from .hardware_loop import CancelReason, MotionStepResult
from .motion import hold_current_position, position_frame
from .workzero import WorkZeroPose

logger = logging.getLogger(__name__)


class ImmediateDoneMotion:
    """幂等回位路径：已在工作零位时立即完成的轻量 motion。

    用于 gozero 目标与当前位形残差过小时的 execution 语义：第一个
    HardwareLoop 周期即 DONE，随后由 GoWorkZero 的 finalizer 启动
    teach manual-clutch LOCK 定住（与 MoveL 路径相同的定住阶段）。
    """

    reject_reason = ""

    @property
    def fraction(self) -> float:
        return 1.0

    def request_cancel(self, reason: CancelReason) -> None:
        del reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        del backend, now
        return MotionStepResult.DONE


# ---- 保守默认（真机验证前不视为定稿数值）----
WORKZERO_KP = np.array([4.0, 12.0, 12.0, 4.0, 3.0, 2.0], dtype=np.float64)
WORKZERO_KD = np.array([0.8, 1.5, 1.5, 0.8, 0.5, 0.4], dtype=np.float64)
WORKZERO_MIN_DURATION_S = 1.0
WORKZERO_MAX_DURATION_S = 60.0
WORKZERO_SETTLE_TIMEOUT_S = 4.0
WORKZERO_SETTLE_TOLERANCE = 0.02  # rad
WORKZERO_SMALL_RESIDUAL = 0.02  # rad：低于此值跳过 PLAN/RUN，直接 MIT settle
WORKZERO_GRIPPER_TOLERANCE = 0.02
WORKZERO_GRIPPER_KP = 5.0
WORKZERO_GRIPPER_KD = 0.5
WORKZERO_CANCEL_DECEL_STEPS = 12
# quintic smoothstep 的峰值导数（位置/加速度），用于有界时间规划
_SMOOTHSTEP_MAX_FIRST_DERIV = 1.875
_SMOOTHSTEP_MAX_SECOND_DERIV = 5.7735


class WorkZeroPhase(str, enum.Enum):
    VALIDATE = "validate"
    PLAN_STREAM = "plan_stream"
    RUNNING = "running"
    SETTLE = "settle"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


def _quintic(tau: float) -> tuple[float, float, float]:
    """五次 smoothstep 及一阶/二阶导数：s, s_dot (1/时间), s_ddot (1/时间²)。"""
    tau = float(np.clip(tau, 0.0, 1.0))
    s = tau**3 * (10.0 - 15.0 * tau + 6.0 * tau**2)
    s_dot = 30.0 * tau**2 * (1.0 - tau) ** 2
    s_ddot = 60.0 * tau * (1.0 - tau) * (1.0 - 2.0 * tau)
    return s, s_dot, s_ddot


class WorkZeroMotion:
    """由 HardwareLoop 每周期推进的工作零位回位状态机。"""

    def __init__(
        self,
        *,
        target: WorkZeroPose,
        timeout_s: float = 30.0,
        kp: np.ndarray = WORKZERO_KP,
        kd: np.ndarray = WORKZERO_KD,
        settle_tolerance: float = WORKZERO_SETTLE_TOLERANCE,
        settle_timeout_s: float = WORKZERO_SETTLE_TIMEOUT_S,
        max_velocity_scale: float = 0.5,
        max_acceleration_scale: float = 0.5,
        operation_name: str = "gozero",
    ) -> None:
        # ---- 构造校验：目标、增益、超时全部在服务端锁定 ----
        joints = np.asarray(target.joints, dtype=np.float64)
        if joints.shape != (6,):
            raise ValueError("WorkZero 目标必须包含 6 个关节")
        if not np.all(np.isfinite(joints)) or not np.isfinite(target.gripper):
            raise ValueError("WorkZero 目标必须全部为有限数值")
        if timeout_s <= 0 or not np.isfinite(timeout_s) or timeout_s > WORKZERO_MAX_DURATION_S:
            raise ValueError(f"timeout_s 必须位于 (0, {WORKZERO_MAX_DURATION_S:g}]")
        kp_arr = np.asarray(kp, dtype=np.float64)
        kd_arr = np.asarray(kd, dtype=np.float64)
        if kp_arr.shape != (6,) or kd_arr.shape != (6,):
            raise ValueError("kp/kd 必须各包含 6 个数值")
        if not np.all(np.isfinite(kp_arr)) or not np.all(np.isfinite(kd_arr)):
            raise ValueError("kp/kd 必须全部为有限数值")
        if np.any(kp_arr <= 0) or np.any(kd_arr <= 0):
            raise ValueError("WorkZeroMotion 要求 kp/kd 全部为正数")
        if settle_tolerance <= 0 or not np.isfinite(settle_tolerance):
            raise ValueError("settle_tolerance 必须为正有限数值")
        if settle_timeout_s <= 0 or not np.isfinite(settle_timeout_s):
            raise ValueError("settle_timeout_s 必须为正有限数值")
        if max_velocity_scale <= 0 or max_velocity_scale > 1.0:
            raise ValueError("max_velocity_scale 必须位于 (0, 1]")
        if max_acceleration_scale <= 0 or max_acceleration_scale > 1.0:
            raise ValueError("max_acceleration_scale 必须位于 (0, 1]")

        self.target = target
        self.timeout_s = float(timeout_s)
        self.kp = kp_arr.copy()
        self.kd = kd_arr.copy()
        self.settle_tolerance = float(settle_tolerance)
        self.settle_timeout_s = float(settle_timeout_s)
        self.max_velocity_scale = float(max_velocity_scale)
        self.max_acceleration_scale = float(max_acceleration_scale)
        self.operation_name = operation_name
        # ---- 审计记录（计划 §6.1）----
        self.reject_reason = ""
        self.errors = np.full(6, np.inf, dtype=np.float64)
        self.final_error: float | None = None
        self.final_gripper_error: float | None = None
        self.max_observed_velocity = np.zeros(6, dtype=np.float64)
        self.max_observed_acceleration = np.zeros(6, dtype=np.float64)
        self.max_observed_torque = np.zeros(6, dtype=np.float64)
        self.start_pose: tuple[float, ...] | None = None
        self.duration_s: float | None = None

        self._phase = WorkZeroPhase.VALIDATE
        self._fraction = 0.0
        self._started_at: float | None = None
        self._settle_started_at: float | None = None
        self._q0: np.ndarray | None = None
        self._delta: np.ndarray | None = None
        self._cancel_reason: CancelReason | None = None
        self._cancel_step = 0
        self._cancel_velocity = np.zeros(6, dtype=np.float64)
        self._prev_velocity: np.ndarray | None = None
        self._prev_step_at: float | None = None
        self._terminal = False
        self._terminal_result = MotionStepResult.RUNNING
        self._lock = threading.Lock()

    @property
    def fraction(self) -> float:
        with self._lock:
            return self._fraction

    @property
    def phase(self) -> WorkZeroPhase:
        return self._phase

    @property
    def terminal(self) -> bool:
        return self._terminal

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        if self._terminal:
            return self._terminal_result
        try:
            return self._step(backend, now)
        except Exception as exc:  # noqa: BLE001 - 状态机异常必须以安全帧收尾
            logger.exception("WorkZeroMotion step 异常: %s", exc)
            self.reject_reason = f"WorkZeroMotion 异常: {exc}"
            self._safe_hold(backend)
            self._finish_terminal(MotionStepResult.FAILED)
            return MotionStepResult.FAILED

    # ----------------------------------------------------------------

    def _step(self, backend: Backend, now: float) -> MotionStepResult:
        states = backend.read_all()
        if len(states) != 7 or not all(state.valid for state in states):
            self.reject_reason = "电机状态无效或连接不完整"
            self._finish_terminal(MotionStepResult.FAILED)
            return MotionStepResult.FAILED
        current = np.asarray([state.position for state in states[:6]], dtype=np.float64)
        measured_velocity = np.asarray([state.velocity for state in states[:6]], dtype=np.float64)
        measured_torque = np.asarray([state.torque for state in states[:6]], dtype=np.float64)
        self.max_observed_velocity = np.maximum(
            self.max_observed_velocity, np.abs(measured_velocity)
        )
        self.max_observed_torque = np.maximum(self.max_observed_torque, np.abs(measured_torque))
        if self._prev_velocity is not None and self._prev_step_at is not None:
            dt = max(0.0, now - self._prev_step_at)
            if dt > 0:
                acceleration = (measured_velocity - self._prev_velocity) / dt
                self.max_observed_acceleration = np.maximum(
                    self.max_observed_acceleration, np.abs(acceleration)
                )
        self._prev_velocity = measured_velocity.copy()
        self._prev_step_at = now

        with self._lock:
            cancel_reason = self._cancel_reason

        if cancel_reason is not None:
            return self._step_cancel(backend, states, current, cancel_reason)

        limits = backend.limits
        target_positions = np.asarray(self.target.joints, dtype=np.float64)
        target_gripper = float(self.target.gripper)

        if self._phase is WorkZeroPhase.VALIDATE:
            if np.any(current < limits.joint_lower) or np.any(current > limits.joint_upper):
                self.reject_reason = "当前关节位置超出软限位，拒绝回位"
                self._safe_hold(backend)
                self._finish_terminal(MotionStepResult.FAILED)
                return MotionStepResult.FAILED
            if not (limits.gripper_lower <= target_gripper <= limits.gripper_upper):
                self.reject_reason = "工作零位夹爪目标超出软限位"
                self._safe_hold(backend)
                self._finish_terminal(MotionStepResult.FAILED)
                return MotionStepResult.FAILED
            self._q0 = current.copy()
            self._delta = target_positions - current
            self.start_pose = tuple(float(value) for value in current)
            self.errors = np.abs(self._delta)
            self._started_at = now
            max_delta = float(np.max(np.abs(self._delta)))
            gripper_delta = abs(target_gripper - states[6].position)
            if max_delta <= WORKZERO_SMALL_RESIDUAL and gripper_delta <= WORKZERO_GRIPPER_TOLERANCE:
                # 冻结的小残差策略：直接进入已验证的 MIT settle，不偷退回单帧位置模式。
                self._phase = WorkZeroPhase.SETTLE
                self._settle_started_at = now
                self._set_fraction(1.0)
            else:
                self._phase = WorkZeroPhase.PLAN_STREAM
            self._prev_velocity = measured_velocity.copy()
            self._prev_step_at = now

        if self._phase is WorkZeroPhase.PLAN_STREAM:
            assert self._delta is not None and self._started_at is not None
            velocity_limits = limits.joint_velocity * self.max_velocity_scale
            acceleration_limits = limits.joint_acceleration * self.max_acceleration_scale
            per_joint_velocity_time = (
                _SMOOTHSTEP_MAX_FIRST_DERIV
                * np.abs(self._delta)
                / np.maximum(velocity_limits, np.finfo(np.float64).eps)
            )
            per_joint_acceleration_time = np.sqrt(
                _SMOOTHSTEP_MAX_SECOND_DERIV
                * np.abs(self._delta)
                / np.maximum(acceleration_limits, np.finfo(np.float64).eps)
            )
            duration = float(
                np.max(
                    [
                        np.max(per_joint_velocity_time),
                        np.max(per_joint_acceleration_time),
                        WORKZERO_MIN_DURATION_S,
                    ]
                )
            )
            if duration > self.timeout_s:
                self.reject_reason = (
                    f"回位行程需要 {duration:.2f}s 超过 timeout {self.timeout_s:g}s；"
                    "拒绝超速硬裁，请调大 timeout_s 或先检查工作零位位形"
                )
                self._safe_hold(backend)
                self._finish_terminal(MotionStepResult.FAILED)
                return MotionStepResult.FAILED
            self.duration_s = duration
            self._phase = WorkZeroPhase.RUNNING

        if self._phase is WorkZeroPhase.RUNNING:
            assert self._q0 is not None and self._delta is not None
            assert self._started_at is not None and self.duration_s is not None
            elapsed = max(0.0, now - self._started_at)
            if elapsed >= self.duration_s:
                self._phase = WorkZeroPhase.SETTLE
                self._settle_started_at = now
                self._set_fraction(1.0)
            else:
                s, s_dot, _ = _quintic(elapsed / self.duration_s)
                commanded = self._q0 + s * self._delta
                commanded_velocity = s_dot * self._delta / self.duration_s
                if np.any(commanded < limits.joint_lower) or np.any(commanded > limits.joint_upper):
                    self.reject_reason = "轨迹位置越出软限位，立即失败"
                    self._safe_hold(backend)
                    self._finish_terminal(MotionStepResult.FAILED)
                    return MotionStepResult.FAILED
                self.errors = np.abs(target_positions - current)
                backend.write_frame(
                    self._mit_frame(commanded, commanded_velocity, target_gripper)
                )
                self._set_fraction(min(1.0, elapsed / self.duration_s))
                return MotionStepResult.RUNNING

        if self._phase is WorkZeroPhase.SETTLE:
            self.errors = np.abs(target_positions - current)
            gripper_error = abs(target_gripper - states[6].position)
            reached = bool(
                np.all(self.errors <= self.settle_tolerance)
                and gripper_error <= WORKZERO_GRIPPER_TOLERANCE
            )
            backend.write_frame(
                self._mit_frame(target_positions, np.zeros(6), target_gripper)
            )
            assert self._settle_started_at is not None
            if reached:
                self.final_error = float(np.max(self.errors))
                self.final_gripper_error = float(gripper_error)
                self._set_fraction(1.0)
                self._finish_terminal(MotionStepResult.DONE)
                return MotionStepResult.DONE
            if now - self._settle_started_at >= self.settle_timeout_s:
                self.reject_reason = (
                    f"工作零位收敛超时：max error {np.max(self.errors):.4g} rad"
                )
                self._safe_hold(backend)
                self._finish_terminal(MotionStepResult.FAILED)
                return MotionStepResult.FAILED
            return MotionStepResult.RUNNING

        raise RuntimeError(f"WorkZeroMotion 进入未知阶段: {self._phase}")

    def _step_cancel(
        self,
        backend: Backend,
        states,
        current: np.ndarray,
        cancel_reason: CancelReason,
    ) -> MotionStepResult:
        if self._cancel_step == 0:
            # 未开始运动（VALIDATE/PLAN_STREAM）：无速度可减，直接安全停止。
            if self._phase in {WorkZeroPhase.VALIDATE, WorkZeroPhase.PLAN_STREAM}:
                self._finish_terminal(MotionStepResult.CANCELLED)
                self.reject_reason = f"已取消: {cancel_reason.value}"
                backend.enter_idle_damping()
                backend.maintain_idle()
                return MotionStepResult.CANCELLED
            self._cancel_velocity = self._prev_velocity.copy() if self._prev_velocity is not None else np.zeros(6)
        self._cancel_step += 1
        scale = max(0.0, 1.0 - self._cancel_step / WORKZERO_CANCEL_DECEL_STEPS)
        backend.write_frame(
            self._mit_frame(
                current,
                self._cancel_velocity * scale,
                float(states[6].position),
            )
        )
        if self._cancel_step < WORKZERO_CANCEL_DECEL_STEPS:
            return MotionStepResult.RUNNING
        self.reject_reason = f"已取消: {cancel_reason.value}"
        backend.enter_idle_damping()
        backend.maintain_idle()
        self._finish_terminal(MotionStepResult.CANCELLED)
        return MotionStepResult.CANCELLED

    def _mit_frame(
        self,
        arm_position: np.ndarray,
        arm_velocity: np.ndarray,
        gripper_position: float,
    ) -> JointFrame:
        """完整 MIT 帧：关节 + 夹爪同一模式（N7 约束）。"""
        return JointFrame(
            mode=FrameMode.POS_VEL_TQE_KP_KD,
            arm_position=np.asarray(arm_position, dtype=np.float64),
            arm_velocity=np.asarray(arm_velocity, dtype=np.float64),
            arm_torque=np.zeros(6),
            arm_kp=self.kp,
            arm_kd=self.kd,
            gripper_position=gripper_position,
            gripper_velocity=0.0,
            gripper_torque=0.0,
            gripper_kp=WORKZERO_GRIPPER_KP,
            gripper_kd=WORKZERO_GRIPPER_KD,
        )

    def _safe_hold(self, backend: Backend) -> None:
        """失败收尾：写一帧 MIT hold（当前位置，零速，保守刚度），随后由
        HardwareLoop 空闲帧接管。严禁使用 position_frame。"""
        try:
            states = backend.read_all()
            if len(states) == 7 and all(state.valid for state in states):
                current = np.asarray([state.position for state in states[:6]], dtype=np.float64)
                backend.write_frame(
                    self._mit_frame(current, np.zeros(6), float(states[6].position))
                )
        except Exception:  # noqa: BLE001
            logger.exception("WorkZeroMotion 安全保持帧写入失败")

    def _set_fraction(self, value: float) -> None:
        with self._lock:
            self._fraction = max(self._fraction, float(value))

    def _finish_terminal(self, result: MotionStepResult) -> None:
        self._phase = {
            MotionStepResult.DONE: WorkZeroPhase.DONE,
            MotionStepResult.CANCELLED: WorkZeroPhase.CANCELLED,
            MotionStepResult.FAILED: WorkZeroPhase.FAILED,
        }[result]
        self._terminal = True
        self._terminal_result = result


WORKZERO_SETTLE_TOLERANCE = 0.03
WORKZERO_SETTLE_TIMEOUT_S = 6.0
WORKZERO_CONTINUOUS_CANCEL_DECEL_STEPS = 12
# 夹爪开爪目标上限：相对 gripper 软限位上界的比例。真机实测爪物理极限
# ≈96%，保存值（如 1.9735）超过极限会持续外推顶机械止点（J7 吃力震动）；
# 目标钳位到 95% 后不再扩展。
WORKZERO_GRIPPER_TARGET_FRACTION = 0.95


class ContinuousTrajectoryMotion:
    """连续插值轨迹 motion（gozero 专用，替代逐点跳变轨迹）。

    真机实测：MoveL 逐点 POS-VEL 轨迹（含 20Hz 抽稀）在固件上表现震颤——每帧
    目标位置跳变（0.05s×2rad/s≈0.1rad），固件 PID 追赶冲击；真机 jog 连续流
    （每周期目标小步推进）则完全平滑。本类把预生成轨迹在每控制周期按时间线性
    插值位置与速度，固件每 5ms 收到微增量目标；末端零速锁定后 settle。
    """

    def __init__(
        self,
        *,
        positions: list[np.ndarray],
        velocities: list[np.ndarray],
        timestamps: list[float],
        max_torque: np.ndarray,
        tolerance: float = WORKZERO_SETTLE_TOLERANCE,
        settle_timeout_s: float = WORKZERO_SETTLE_TIMEOUT_S,
        operation_name: str = "gozero-continuous",
    ) -> None:
        if not positions or len(positions) != len(velocities) or len(positions) != len(timestamps):
            raise ValueError("轨迹位置、速度、时间戳长度必须一致且非空")
        if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:], strict=False)):
            raise ValueError("轨迹时间戳必须单调递增")
        self.positions = [np.asarray(value, dtype=np.float64).copy() for value in positions]
        self.velocities = [np.asarray(value, dtype=np.float64).copy() for value in velocities]
        self.timestamps = np.asarray(timestamps, dtype=np.float64)
        self.max_torque = np.asarray(max_torque, dtype=np.float64).copy()
        self.tolerance = tolerance
        self.settle_timeout_s = settle_timeout_s
        self.operation_name = operation_name
        self.reject_reason = ""
        self.errors = np.full(6, np.inf, dtype=np.float64)
        self._fraction = 0.0
        self._started_at: float | None = None
        self._settle_started_at: float | None = None
        self._cancel_reason: CancelReason | None = None
        self._deceleration_step: int | None = None
        self._deceleration_velocity = np.zeros(6, dtype=np.float64)
        self._lock = threading.Lock()

    @property
    def fraction(self) -> float:
        with self._lock:
            return self._fraction

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        states = backend.read_all()
        if len(states) != 7 or not all(state.valid for state in states):
            backend.stop()
            self.reject_reason = "电机状态无效或连接不完整"
            return MotionStepResult.FAILED
        current = np.array([state.position for state in states[:6]], dtype=np.float64)
        if self._started_at is None:
            self._started_at = now

        with self._lock:
            cancel_reason = self._cancel_reason
        if cancel_reason is not None:
            return self._step_cancel(backend, states[6].position, current, cancel_reason)

        elapsed = max(0.0, now - self._started_at)
        if elapsed < self.timestamps[-1]:
            # ---- 连续插值：每控制周期平滑推进目标（位置与速度都插值）----
            index = int(np.searchsorted(self.timestamps, elapsed, side="right"))
            index = min(max(index, 1), len(self.timestamps) - 1)
            t0 = float(self.timestamps[index - 1])
            t1 = float(self.timestamps[index])
            alpha = (elapsed - t0) / max(t1 - t0, np.finfo(np.float64).eps)
            alpha = float(np.clip(alpha, 0.0, 1.0))
            commanded = self.positions[index - 1] * (1.0 - alpha) + self.positions[index] * alpha
            commanded_velocity = (
                self.velocities[index - 1] * (1.0 - alpha) + self.velocities[index] * alpha
            )
            self.errors = np.abs(self.positions[-1] - current)
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=commanded,
                    arm_velocity=commanded_velocity,
                    arm_max_torque=self.max_torque,
                    gripper_position=states[6].position,
                )
            )
            with self._lock:
                self._fraction = max(
                    self._fraction,
                    min(1.0, elapsed / max(self.timestamps[-1], np.finfo(np.float64).eps)),
                )
            return MotionStepResult.RUNNING

        # ---- settle：末点零速锁定，误差收敛后 DONE ----
        target = self.positions[-1]
        self.errors = np.abs(target - current)
        if self._settle_started_at is None:
            self._settle_started_at = now
        backend.write_frame(
            position_frame(
                backend,
                arm_position=target,
                arm_velocity=np.zeros(6),
                arm_max_torque=self.max_torque,
                gripper_position=states[6].position,
            )
        )
        with self._lock:
            self._fraction = 1.0
        if np.all(self.errors <= self.tolerance):
            return MotionStepResult.DONE
        if now - self._settle_started_at >= self.settle_timeout_s:
            hold_current_position(backend)
            self.reject_reason = f"{self.operation_name} 末点收敛超时"
            return MotionStepResult.FAILED
        return MotionStepResult.RUNNING

    def _step_cancel(
        self,
        backend: Backend,
        gripper_position: float,
        current: np.ndarray,
        cancel_reason: CancelReason,
    ) -> MotionStepResult:
        if self._deceleration_step is None:
            self._deceleration_velocity = current.copy()
            self._deceleration_step = 0
        self._deceleration_step += 1
        scale = max(0.0, 1.0 - self._deceleration_step / WORKZERO_CONTINUOUS_CANCEL_DECEL_STEPS)
        backend.write_frame(
            position_frame(
                backend,
                arm_position=current,
                arm_velocity=np.maximum(self._deceleration_velocity * scale, 1e-3),
                arm_max_torque=self.max_torque,
                gripper_position=gripper_position,
            )
        )
        if self._deceleration_step < WORKZERO_CONTINUOUS_CANCEL_DECEL_STEPS:
            return MotionStepResult.RUNNING
        self.reject_reason = f"运动已取消: {cancel_reason.value}"
        return MotionStepResult.CANCELLED
