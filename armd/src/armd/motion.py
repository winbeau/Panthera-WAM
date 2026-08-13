"""M3 非阻塞关节运动状态机。"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .backend import (
    Backend,
    BackendError,
    FrameMode,
    JointFrame,
    filter_idle_velocity,
    idle_damping_frame,
    smooth_idle_damping_frame,
)
from .hardware_loop import CancelReason, MotionStepResult
from .teach import PlaybackFrame

logger = logging.getLogger(__name__)

POSITION_HOLD_SPEED = 0.1
JOG_FRESHNESS_S = 0.25
JOG_LIMIT_MARGIN = 0.02
# Jog is deliberately implemented as a short position/velocity target rather
# than raw MODE_VELOCITY.  The latter removes position hold from the other
# gravity-loaded joints on this shared CAN frame and makes a button press an
# instantaneous velocity step.  Keep the target close enough that feedback
# remains authoritative, while still above the encoder quantisation noise.
JOG_TARGET_LOOKAHEAD_S = 0.08
JOG_DECELERATION_FACTOR = 4.0
JOG_ZERO_EPSILON = 1e-4
MIT_FRESHNESS_S = 0.12
TEACH_VEL_THRESHOLD_S = 0.02
TEACH_TAU_LIMIT = np.array([15.0, 30.0, 30.0, 15.0, 5.0, 5.0], dtype=np.float64)
# 官方 SDK 的阻抗示例使用 K=[4,10,10,2,2,1]；显式 lock 是确定性
# 位置保持，不应复用面向自动判定的保守 kp_hold。承重轴 J2/J3 再提升到 20，
# 进一步压低残余误差导致的稳态偏移（偏移≈残差/kp）。
MANUAL_CLUTCH_KP_HOLD = np.array([4.0, 20.0, 20.0, 2.0, 2.0, 1.0], dtype=np.float64)
MANUAL_CLUTCH_HOLD_RAMP_TIME_S = 0.08
MANUAL_CLUTCH_RELEASE_RAMP_TIME_S = 0.08
# 显式 HOLD 阻尼下限：不低于拖动阻尼，并至少达到 kp*0.08 防止欠阻尼振荡。
MANUAL_CLUTCH_KD_MIN_RATIO = 0.08
GRIPPER_POSITION_TORQUE_FRACTION = 0.8
GRIPPER_POSITION_MAX_KP = 5.0
GRIPPER_POSITION_MAX_KD = 0.5


class AutoHoldState(str, enum.Enum):
    DRAG = "drag"
    STILL_DETECT = "still_detect"
    HOLD = "hold"
    RELEASE = "release"
    SAFE_HOLD = "safe_hold"


class TeachClutchCommand(str, enum.Enum):
    LOCK = "lock"
    DRAG = "drag"


@dataclass(frozen=True, slots=True)
class AutoHoldConfig:
    """Auto-Hold（静止自动锁位）配置：松手检测 + 位置保持 + 平滑退出。

    重力补偿负责「拖动轻」，Auto-Hold 只负责「松手停住」——不依赖
    重力残差为零。所有 kp/kd 切换均用 smoothstep 渐变，禁止瞬间跳变。
    """

    enabled: bool = True
    still_velocity_threshold: float = 0.02  # rad/s，全部关节低于此值视为静止
    release_velocity_threshold: float = 0.04  # rad/s，任一关节超过此值视为重新拖动
    still_duration: float = 0.20  # s，静止持续时长确认松手
    hold_ramp_time: float = 0.40  # s，kp 从 0 渐变到 kp_hold
    release_ramp_time: float = 0.20  # s，kp 从 kp_hold 渐变回 0
    # 保守逐关节保持刚度；重力残差应在独立标定层修正，不靠盲目增大 kp
    kp_hold: tuple[float, ...] = (1.0, 2.0, 2.0, 1.0, 0.8, 0.8)
    kd_drag: tuple[float, ...] | None = None  # 拖动阻尼；None=沿用 TeachMotion.kd
    kd_hold: tuple[float, ...] = (0.4, 0.8, 0.8, 0.4, 0.2, 0.2)  # 逐关节保持阻尼
    velocity_filter_tau_s: float = 0.03  # 速度判定用低通时间常数

    def __post_init__(self) -> None:
        for name in (
            "still_velocity_threshold",
            "release_velocity_threshold",
            "still_duration",
            "hold_ramp_time",
            "release_ramp_time",
        ):
            value = getattr(self, name)
            if value <= 0 or not np.isfinite(value):
                raise ValueError(f"auto-hold {name} 必须为正有限数值")
        if self.release_velocity_threshold <= self.still_velocity_threshold:
            raise ValueError("release_velocity_threshold 必须大于 still_velocity_threshold")
        for name in ("kp_hold", "kd_hold"):
            values = np.asarray(getattr(self, name), dtype=np.float64)
            if values.shape != (6,) or not np.all(np.isfinite(values)) or np.any(values < 0):
                raise ValueError(f"auto-hold {name} 必须为 6 个非负有限数值")
        if self.kd_drag is not None:
            values = np.asarray(self.kd_drag, dtype=np.float64)
            if values.shape != (6,) or not np.all(np.isfinite(values)) or np.any(values < 0):
                raise ValueError("auto-hold kd_drag 必须为 6 个非负有限数值")
        if self.velocity_filter_tau_s <= 0 or not np.isfinite(self.velocity_filter_tau_s):
            raise ValueError("auto-hold velocity_filter_tau_s 必须为正有限数值")


def _smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def _gravity_params(
    scale: float | np.ndarray,
    high: float | np.ndarray | None,
    breakpoint: float | np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """解析重力补偿分段参数：低区 scale、高区 scale（默认=低区）、断点（默认 inf）。"""
    low = np.asarray(scale, dtype=np.float64)
    if low.shape == ():
        low = np.full(6, float(low))
    if low.shape != (6,) or not np.all(np.isfinite(low)) or np.any(low <= 0):
        raise ValueError("gravity_scale 必须是正有限标量或 6 个正有限数值")
    if high is None:
        high_array = low.copy()
    else:
        high_array = np.asarray(high, dtype=np.float64)
        if high_array.shape == ():
            high_array = np.full(6, float(high_array))
        if high_array.shape != (6,) or not np.all(np.isfinite(high_array)) or np.any(high_array <= 0):
            raise ValueError("gravity_scale_high 必须是正有限标量或 6 个正有限数值")
    if breakpoint is None:
        breakpoint_array = np.full(6, np.inf)
    else:
        breakpoint_array = np.asarray(breakpoint, dtype=np.float64)
        if breakpoint_array.shape == ():
            breakpoint_array = np.full(6, float(breakpoint_array))
        finite = breakpoint_array[np.isfinite(breakpoint_array)]
        if breakpoint_array.shape != (6,) or np.any(finite <= 0):
            raise ValueError("gravity_breakpoint 必须是正值或 inf（inf 表示不分段）")
    return low.copy(), high_array.copy(), breakpoint_array.copy()


def position_frame(
    backend: Backend,
    *,
    arm_position: np.ndarray,
    arm_velocity: np.ndarray,
    gripper_position: float,
    gripper_velocity: float = POSITION_HOLD_SPEED,
    arm_max_torque: np.ndarray | None = None,
    gripper_max_torque: float | None = None,
) -> JointFrame:
    safe_gripper_position = float(
        np.clip(gripper_position, backend.limits.gripper_lower, backend.limits.gripper_upper)
    )
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE,
        arm_position=arm_position,
        arm_velocity=arm_velocity,
        arm_max_torque=backend.limits.joint_torque if arm_max_torque is None else arm_max_torque,
        gripper_position=safe_gripper_position,
        gripper_velocity=gripper_velocity,
        gripper_max_torque=(
            backend.limits.gripper_torque if gripper_max_torque is None else gripper_max_torque
        ),
    )


def gripper_position_frame(
    backend: Backend,
    *,
    arm_position: np.ndarray,
    arm_filtered_velocity: np.ndarray,
    gripper_position: float,
    gripper_current_position: float,
    gripper_current_velocity: float,
    gripper_velocity: float,
    gripper_max_torque: float,
) -> JointFrame:
    """用同一 MIT 帧控制夹爪，同时让六个关节保持零刚度阻尼。

    夹爪与机械臂共用 CAN TX 帧，不能让夹爪使用 POS-VEL 而关节使用 MIT。
    因此把 POS-VEL 风格的夹爪请求转换成逐周期受限 MIT 阻抗：80% 力矩预算
    分配给当前位置误差，20% 分配给当前速度误差，并限制 kp/kd 不超过 SDK
    回放默认量级。六轴沿用滤波速度生成的软件阻尼力矩，固件 kp/kd 保持为零，
    避免夹爪动作期间重新引入速度量化导致的 J6 抽搐。
    """
    limits = backend.limits
    safe_position = float(np.clip(gripper_position, limits.gripper_lower, limits.gripper_upper))
    position_budget = gripper_max_torque * GRIPPER_POSITION_TORQUE_FRACTION
    velocity_budget = gripper_max_torque - position_budget
    position_error = abs(safe_position - gripper_current_position)
    gripper_kp = min(
        GRIPPER_POSITION_MAX_KP,
        position_budget / max(position_error, np.finfo(np.float64).eps),
    )
    direction = float(np.sign(safe_position - gripper_current_position))
    desired_velocity = direction * gripper_velocity
    velocity_error = abs(desired_velocity - gripper_current_velocity)
    gripper_kd = min(
        GRIPPER_POSITION_MAX_KD,
        velocity_budget / max(velocity_error, np.finfo(np.float64).eps),
    )
    arm_idle = smooth_idle_damping_frame(
        limits,
        arm_position,
        arm_filtered_velocity,
        gripper_current_position,
    )
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE_KP_KD,
        arm_position=arm_idle.arm_position,
        arm_velocity=arm_idle.arm_velocity,
        arm_torque=arm_idle.arm_torque,
        arm_kp=arm_idle.arm_kp,
        arm_kd=arm_idle.arm_kd,
        gripper_position=safe_position,
        gripper_velocity=desired_velocity,
        gripper_torque=0.0,
        gripper_kp=gripper_kp,
        gripper_kd=gripper_kd,
    )


def hold_current_position(backend: Backend) -> None:
    states = backend.read_all()
    if len(states) != 7 or not all(state.valid for state in states):
        backend.stop()
        return
    backend.write_frame(
        position_frame(
            backend,
            arm_position=np.array([state.position for state in states[:6]], dtype=np.float64),
            arm_velocity=np.full(6, POSITION_HOLD_SPEED),
            gripper_position=states[6].position,
        )
    )


class GripperPositionMotion:
    """受限 MIT 夹爪位置运动；机械臂始终保持零刚度阻尼。"""

    def __init__(
        self,
        *,
        position: float,
        velocity: float,
        max_torque: float,
        tolerance: float = 0.01,
    ) -> None:
        self.position = float(position)
        self.velocity = float(velocity)
        self.max_torque = float(max_torque)
        self.tolerance = float(tolerance)
        self.timeout_s: float | None = None
        self.reject_reason = ""
        self._started_at: float | None = None
        self._arm_filter_updated_at: float | None = None
        self._arm_filtered_velocity = np.zeros(6, dtype=np.float64)
        self._cancel_reason: CancelReason | None = None
        self._lock = threading.Lock()

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        states = backend.read_all()
        if len(states) != 7 or not all(state.valid for state in states):
            backend.stop()
            self.reject_reason = "电机状态无效或连接不完整"
            return MotionStepResult.FAILED
        if self._started_at is None:
            self._started_at = now
            distance = abs(self.position - states[6].position)
            self.timeout_s = max(2.0, 4.0 * distance / max(self.velocity, 0.05) + 2.0)
        with self._lock:
            cancel_reason = self._cancel_reason
        arm_position = np.asarray([state.position for state in states[:6]], dtype=np.float64)
        arm_velocity = np.asarray([state.velocity for state in states[:6]], dtype=np.float64)
        dt_s = 0.0 if self._arm_filter_updated_at is None else max(0.0, now - self._arm_filter_updated_at)
        self._arm_filtered_velocity = filter_idle_velocity(
            self._arm_filtered_velocity,
            arm_velocity,
            dt_s=dt_s,
        )
        self._arm_filter_updated_at = now
        if cancel_reason is not None:
            backend.enter_idle_damping()
            backend.maintain_idle()
            self.reject_reason = f"夹爪运动已取消: {cancel_reason.value}"
            return MotionStepResult.CANCELLED

        error = self.position - states[6].position
        if abs(error) <= self.tolerance:
            backend.enter_idle_damping()
            backend.maintain_idle()
            return MotionStepResult.DONE
        if (
            self._started_at is not None
            and self.timeout_s is not None
            and now - self._started_at >= self.timeout_s
        ):
            backend.enter_idle_damping()
            backend.maintain_idle()
            self.reject_reason = "夹爪运动超时"
            return MotionStepResult.FAILED

        backend.write_frame(
            gripper_position_frame(
                backend,
                arm_position=arm_position,
                arm_filtered_velocity=self._arm_filtered_velocity,
                gripper_position=self.position,
                gripper_current_position=states[6].position,
                gripper_current_velocity=states[6].velocity,
                gripper_velocity=self.velocity,
                gripper_max_torque=self.max_torque,
            )
        )
        return MotionStepResult.RUNNING


class JointPositionMotion:
    """按 SDK 语义只下发一次 POS-VEL 目标，随后逐周期轮询到位。"""

    def __init__(
        self,
        *,
        positions: np.ndarray,
        velocities: np.ndarray,
        max_torque: np.ndarray,
        tolerance: float,
        deadline: float,
    ) -> None:
        self.positions = np.asarray(positions, dtype=np.float64).copy()
        self.velocities = np.asarray(velocities, dtype=np.float64).copy()
        self.max_torque = np.asarray(max_torque, dtype=np.float64).copy()
        self.tolerance = tolerance
        self.deadline = deadline
        self.errors = np.full(6, np.inf, dtype=np.float64)
        self.reject_reason = ""
        self._command_sent = False
        self._cancel_reason: CancelReason | None = None
        self._lock = threading.Lock()

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
        self.errors = np.abs(self.positions - current)
        with self._lock:
            cancel_reason = self._cancel_reason
        if cancel_reason is not None:
            hold_current_position(backend)
            self.reject_reason = f"运动已取消: {cancel_reason.value}"
            return MotionStepResult.CANCELLED
        if not self._command_sent:
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=self.positions,
                    arm_velocity=self.velocities,
                    arm_max_torque=self.max_torque,
                    gripper_position=states[6].position,
                )
            )
            self._command_sent = True
            if np.all(self.errors <= self.tolerance):
                return MotionStepResult.DONE
            return MotionStepResult.RUNNING
        if np.all(self.errors <= self.tolerance):
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=self.positions,
                    arm_velocity=np.zeros(6),
                    arm_max_torque=self.max_torque,
                    gripper_position=states[6].position,
                )
            )
            return MotionStepResult.DONE
        if now >= self.deadline:
            hold_current_position(backend)
            self.reject_reason = "等待关节到位超时"
            return MotionStepResult.FAILED

        return MotionStepResult.RUNNING


class JointJogMotion:
    """受加速度限制的短前瞻位置点动。

    Panthera-HT 的七个电机共享一帧 CAN 指令。裸 ``MODE_VELOCITY`` 会让
    未点动的承重关节只收到零速度，没有位置保持；同时按钮按下会把速度
    从 0 瞬时跳到目标值。J2/J3 因此可能出现明显冲击。这里改为每周期
    下发 ``POS_VEL_TQE``：目标只向当前速度前瞻一小段，并按 SDK 配置的
    加速度限幅；过期/停止时受控减速到零，再进入空闲阻尼。
    """

    def __init__(
        self,
        *,
        freshness_s: float = JOG_FRESHNESS_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.freshness_s = freshness_s
        self._clock = clock
        self._velocities = np.zeros(6, dtype=np.float64)
        self._applied_velocities = np.zeros(6, dtype=np.float64)
        self._last_command_at = float("-inf")
        self._last_step_at: float | None = None
        self._cancel_reason: CancelReason | None = None
        self._limit_hit = np.zeros(6, dtype=np.bool_)
        self._lock = threading.Lock()

    @property
    def limit_hit(self) -> tuple[bool, ...]:
        with self._lock:
            return tuple(bool(value) for value in self._limit_hit)

    def update(self, velocities: np.ndarray) -> None:
        values = np.asarray(velocities, dtype=np.float64)
        if values.shape != (6,) or not np.all(np.isfinite(values)):
            raise ValueError("JointJog.velocities 必须是 6 个有限数值")
        with self._lock:
            self._velocities = values.copy()
            self._last_command_at = self._clock()

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        states = backend.read_all()
        if len(states) != 7 or not all(state.valid for state in states):
            backend.stop()
            return MotionStepResult.FAILED

        with self._lock:
            cancel_reason = self._cancel_reason
            requested_velocities = self._velocities.copy()
            stale = now - self._last_command_at > self.freshness_s
        if cancel_reason is not None:
            requested_velocities.fill(0.0)
        elif stale:
            requested_velocities.fill(0.0)

        if self._last_step_at is None:
            dt_s = 0.0
        else:
            dt_s = max(0.0, now - self._last_step_at)
        self._last_step_at = now

        acceleration = np.asarray(backend.limits.joint_acceleration, dtype=np.float64)
        delta_limit = acceleration * dt_s
        if cancel_reason is not None or stale:
            delta_limit *= JOG_DECELERATION_FACTOR
        delta = requested_velocities - self._applied_velocities
        self._applied_velocities += np.clip(delta, -delta_limit, delta_limit)
        velocities = self._applied_velocities.copy()

        positions = np.array([state.position for state in states[:6]], dtype=np.float64)
        at_upper = positions >= backend.limits.joint_upper - JOG_LIMIT_MARGIN
        at_lower = positions <= backend.limits.joint_lower + JOG_LIMIT_MARGIN
        limit_hit = (at_upper & (requested_velocities > 0)) | (at_lower & (requested_velocities < 0))
        # Never let a deceleration ramp carry a joint through the soft-limit
        # margin after the command has already been blocked.
        limit_hit |= (at_upper & (velocities > 0)) | (at_lower & (velocities < 0))
        velocities[limit_hit] = 0.0
        self._applied_velocities[limit_hit] = 0.0
        if np.any(np.abs(velocities) > backend.limits.joint_velocity):
            raise BackendError("JointJog 速度超过软限位")

        with self._lock:
            self._limit_hit = limit_hit
        target_positions = np.clip(
            positions + velocities * JOG_TARGET_LOOKAHEAD_S,
            backend.limits.joint_lower,
            backend.limits.joint_upper,
        )
        # POS-VEL's velocity field is a non-negative speed bound; direction
        # comes from target_position.  Keep a small hold speed for stationary
        # gravity-loaded joints so they do not silently lose position hold.
        absolute_velocity = np.abs(velocities)
        speed = np.where(
            absolute_velocity > JOG_ZERO_EPSILON,
            absolute_velocity,
            POSITION_HOLD_SPEED,
        )
        backend.write_frame(
            position_frame(
                backend,
                arm_position=target_positions,
                arm_velocity=speed,
                arm_max_torque=backend.limits.joint_torque,
                gripper_position=states[6].position,
                gripper_velocity=POSITION_HOLD_SPEED,
            )
        )
        if cancel_reason is not None and np.all(np.abs(self._applied_velocities) <= JOG_ZERO_EPSILON):
            backend.enter_idle_damping()
            backend.maintain_idle()
            return MotionStepResult.CANCELLED
        return MotionStepResult.RUNNING


class JointMITMotion:
    """流式 MIT 阻抗控制；120ms 无新指令即退回柔顺阻尼。"""

    def __init__(
        self,
        *,
        freshness_s: float = MIT_FRESHNESS_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.freshness_s = freshness_s
        self._clock = clock
        self._command: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None
        self._last_command_at = float("-inf")
        self._cancel_reason: CancelReason | None = None
        self._lock = threading.Lock()

    def update(
        self,
        *,
        positions: np.ndarray,
        velocities: np.ndarray,
        torques: np.ndarray,
        kp: np.ndarray,
        kd: np.ndarray,
    ) -> None:
        values = tuple(
            np.asarray(value, dtype=np.float64).copy() for value in (positions, velocities, torques, kp, kd)
        )
        if any(value.shape != (6,) or not np.all(np.isfinite(value)) for value in values):
            raise ValueError("JointMIT 的 pos/vel/tqe/kp/kd 必须分别包含 6 个有限数值")
        with self._lock:
            self._command = values
            self._last_command_at = self._clock()

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        states = backend.read_all()
        if len(states) != 7 or not all(state.valid for state in states):
            backend.stop()
            return MotionStepResult.FAILED
        with self._lock:
            command = self._command
            cancel_reason = self._cancel_reason
            stale = now - self._last_command_at > self.freshness_s
        if command is None or cancel_reason is not None or stale:
            backend.write_frame(
                idle_damping_frame(
                    backend.limits,
                    np.array([state.position for state in states[:6]], dtype=np.float64),
                    states[6].position,
                )
            )
            return MotionStepResult.CANCELLED

        positions, velocities, torques, kp, kd = command
        backend.write_frame(
            JointFrame(
                mode=FrameMode.POS_VEL_TQE_KP_KD,
                arm_position=positions,
                arm_velocity=velocities,
                arm_torque=torques,
                arm_kp=kp,
                arm_kd=kd,
                gripper_position=states[6].position,
                gripper_velocity=0.0,
                gripper_torque=0.0,
                gripper_kp=0.0,
                gripper_kd=0.3,
            )
        )
        return MotionStepResult.RUNNING


class CartesianTrajectoryMotion:
    """按绝对时间戳执行 POS-VEL 轨迹，并提供单调进度与 12 周期取消减速。"""

    def __init__(
        self,
        *,
        positions: list[np.ndarray],
        velocities: list[np.ndarray],
        timestamps: list[float],
        max_torque: np.ndarray,
        tolerance: float = 0.001,
        settle_timeout_s: float = 2.0,
        operation_name: str = "moveL",
    ) -> None:
        if not positions or len(positions) != len(velocities) or len(positions) != len(timestamps):
            raise ValueError("笛卡尔轨迹位置、速度、时间戳长度必须一致且非空")
        if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:], strict=False)):
            raise ValueError("笛卡尔轨迹时间戳必须单调递增")
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
        self._cancel_reason: CancelReason | None = None
        self._deceleration_step: int | None = None
        self._deceleration_velocity = np.zeros(6, dtype=np.float64)
        self._last_index = -1
        self._settle_command_sent = False
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

        elapsed = now - self._started_at
        index = min(
            max(0, int(np.searchsorted(self.timestamps, elapsed, side="right")) - 1),
            len(self.positions) - 1,
        )
        if elapsed < self.timestamps[-1]:
            if index != self._last_index:
                backend.write_frame(
                    position_frame(
                        backend,
                        arm_position=self.positions[index],
                        arm_velocity=self.velocities[index],
                        arm_max_torque=self.max_torque,
                        gripper_position=states[6].position,
                    )
                )
                self._last_index = index
            with self._lock:
                self._fraction = max(
                    self._fraction,
                    min(1.0, elapsed / max(self.timestamps[-1], np.finfo(np.float64).eps)),
                )
            return MotionStepResult.RUNNING

        target = self.positions[-1]
        self.errors = np.abs(target - current)
        if np.all(self.errors <= self.tolerance):
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
            return MotionStepResult.DONE
        if elapsed >= self.timestamps[-1] + self.settle_timeout_s:
            hold_current_position(backend)
            self.reject_reason = f"{self.operation_name} 末点收敛超时"
            return MotionStepResult.FAILED
        if not self._settle_command_sent:
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=target,
                    arm_velocity=np.full(6, POSITION_HOLD_SPEED),
                    arm_max_torque=self.max_torque,
                    gripper_position=states[6].position,
                )
            )
            self._settle_command_sent = True
        return MotionStepResult.RUNNING

    def _step_cancel(
        self,
        backend: Backend,
        gripper_position: float,
        current: np.ndarray,
        cancel_reason: CancelReason,
    ) -> MotionStepResult:
        if self._deceleration_step is None:
            self._deceleration_velocity = np.abs(self.velocities[self._last_index])
            self._deceleration_step = 0
        self._deceleration_step += 1
        scale = max(0.0, 1.0 - self._deceleration_step / 12.0)
        backend.write_frame(
            position_frame(
                backend,
                arm_position=current,
                arm_velocity=np.maximum(self._deceleration_velocity * scale, 1e-3),
                arm_max_torque=self.max_torque,
                gripper_position=gripper_position,
            )
        )
        if self._deceleration_step < 12:
            return MotionStepResult.RUNNING
        self.reject_reason = f"运动已取消: {cancel_reason.value}"
        return MotionStepResult.CANCELLED


class TeachMotion:
    """重力/摩擦前馈的连续拖动示教模式（含 Auto-Hold 静止自动锁位）。"""

    def __init__(
        self,
        *,
        kp: np.ndarray,
        kd: np.ndarray,
        fc: np.ndarray,
        fv: np.ndarray,
        tau_limit: np.ndarray = TEACH_TAU_LIMIT,
        vel_threshold: float = TEACH_VEL_THRESHOLD_S,
        gravity_scale: float | np.ndarray = 1.0,
        gravity_scale_high: float | np.ndarray | None = None,
        gravity_breakpoint: float | np.ndarray | None = None,
        gravity_segmented: bool = False,
        gravity_residual: float | np.ndarray = 0.0,
        auto_hold: AutoHoldConfig | None = None,
        manual_clutch: bool = False,
        safe_hold_time_s: float = 10.0,
    ) -> None:
        self.kp = np.asarray(kp, dtype=np.float64).copy()
        self.kd = np.asarray(kd, dtype=np.float64).copy()
        self.fc = np.asarray(fc, dtype=np.float64).copy()
        self.fv = np.asarray(fv, dtype=np.float64).copy()
        self.tau_limit = np.asarray(tau_limit, dtype=np.float64).copy()
        vectors = (self.kp, self.kd, self.fc, self.fv, self.tau_limit)
        if any(value.shape != (6,) or not np.all(np.isfinite(value)) for value in vectors):
            raise ValueError("示教控制参数必须各包含 6 个有限数值")
        if np.any(self.kp < 0) or np.any(self.kd < 0) or np.any(self.tau_limit <= 0):
            raise ValueError("示教 kp/kd 不得为负，tau_limit 必须为正")
        if vel_threshold < 0 or not np.isfinite(vel_threshold):
            raise ValueError("vel_threshold 必须是非负有限数值")
        self.gravity_scale, self.gravity_scale_high, self.gravity_breakpoint = _gravity_params(
            gravity_scale, gravity_scale_high, gravity_breakpoint
        )
        self.gravity_segmented = bool(gravity_segmented)
        residual = np.asarray(gravity_residual, dtype=np.float64)
        if residual.shape == ():
            residual = np.full(6, float(residual))
        if residual.shape != (6,) or not np.all(np.isfinite(residual)):
            raise ValueError("gravity_residual 必须是有限标量或 6 个有限数值")
        self.gravity_residual = residual.copy()
        self.vel_threshold = float(vel_threshold)
        if not np.isfinite(safe_hold_time_s) or safe_hold_time_s <= 0:
            raise ValueError("safe_hold_time_s 必须为正有限数值")
        self.safe_hold_time_s = float(safe_hold_time_s)
        self.reject_reason = ""
        self._cancel_reason: CancelReason | None = None
        self._lock = threading.Lock()

        # ---- Auto-Hold 状态机（None=默认启用；enabled=False 禁用并回退原行为）----
        self.auto_hold_cfg = auto_hold if auto_hold is not None else AutoHoldConfig()
        self.manual_clutch = bool(manual_clutch)
        if self.manual_clutch and not self.auto_hold_cfg.enabled:
            raise ValueError("manual_clutch 需要启用 auto-hold 位置保持")
        self._clutch_request: TeachClutchCommand | None = None
        self._hold_state = AutoHoldState.DRAG
        self._state_since: float | None = None
        self._still_since: float | None = None
        self._q_hold: np.ndarray | None = None
        self._kp_now = np.zeros(6, dtype=np.float64)
        self._hold_kp_start = np.zeros(6, dtype=np.float64)
        self._release_kp_start = np.zeros(6, dtype=np.float64)
        self._release_kd_start = np.zeros(6, dtype=np.float64)
        if self.auto_hold_cfg.kd_drag is None:
            self._kd_drag_now = self.kd.copy()
        else:
            self._kd_drag_now = np.asarray(self.auto_hold_cfg.kd_drag, dtype=np.float64).copy()
        # 显式 HOLD 的阻尼不得低于拖动阻尼，并至少达到 kp*0.08 防止欠阻尼振荡。
        self._manual_kd_hold = np.maximum.reduce(
            [
                np.asarray(self.auto_hold_cfg.kd_hold, dtype=np.float64),
                self._kd_drag_now,
                MANUAL_CLUTCH_KP_HOLD * MANUAL_CLUTCH_KD_MIN_RATIO,
            ]
        )
        self._safe_hold_until: float | None = None
        self._filtered_velocity = np.zeros(6, dtype=np.float64)
        self._velocity_filter_updated_at: float | None = None

    @property
    def auto_hold_state(self) -> AutoHoldState:
        return self._hold_state

    @property
    def hold_position(self) -> np.ndarray | None:
        return None if self._q_hold is None else self._q_hold.copy()

    @property
    def hold_kp(self) -> np.ndarray:
        return self._kp_now.copy()

    @property
    def safe_holding(self) -> bool:
        """teach 取消后的安全保持阶段：重力前馈+位置保持仍在运行。"""
        return self._hold_state is AutoHoldState.SAFE_HOLD

    @property
    def fraction(self) -> float:
        return 0.0

    def request_clutch(self, command: TeachClutchCommand) -> None:
        if not self.manual_clutch:
            raise ValueError("当前 teach 未启用显式 clutch")
        if not isinstance(command, TeachClutchCommand):
            raise ValueError("clutch 命令必须为 lock 或 drag")
        with self._lock:
            self._clutch_request = command

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        states = backend.read_all()
        if len(states) != 7 or not all(state.valid for state in states):
            backend.stop()
            self.reject_reason = "电机状态无效或连接不完整"
            return MotionStepResult.FAILED
        with self._lock:
            cancel_reason = self._cancel_reason
        positions = np.asarray([state.position for state in states[:6]], dtype=np.float64)
        velocities = np.asarray([state.velocity for state in states[:6]], dtype=np.float64)
        if cancel_reason is not None:
            if self.manual_clutch and self._hold_state is not AutoHoldState.SAFE_HOLD:
                # 显式离合 teach 被取消（lease 过期/watchdog/客户端停止）时，
                # 不立即切软：先锚定当前位形并保持重力前馈与位置刚度一段时间，
                # 避免承重关节失去前馈后直接坠落。
                self._q_hold = positions.copy()
                self._still_since = None
                self._hold_kp_start = np.zeros(6, dtype=np.float64)
                self._safe_hold_until = now + self.safe_hold_time_s
                self._enter_state(
                    AutoHoldState.SAFE_HOLD,
                    now,
                    f"cancel -> SAFE_HOLD ({cancel_reason.value}, {self.safe_hold_time_s:g}s)",
                )
            if not (self._hold_state is AutoHoldState.SAFE_HOLD and now < (self._safe_hold_until or 0.0)):
                backend.write_frame(idle_damping_frame(backend.limits, positions, states[6].position))
                self.reject_reason = f"示教已停止: {cancel_reason.value}"
                return MotionStepResult.CANCELLED

        if self.auto_hold_cfg.enabled:
            kp, kd, cmd_positions, cmd_velocities = self._auto_hold_step(positions, velocities, now)
        else:
            kp, kd, cmd_positions, cmd_velocities = self.kp, self.kd, positions, np.zeros(6)

        # HOLD/RELEASE/SAFE_HOLD 的前馈锚定在锁定位形：重力项不能随漂移后的 q 追着走，
        # 摩擦项也不能把当前漂移速度变成同方向助推。DRAG 仍使用实时 q/v。
        if self.manual_clutch and self._q_hold is not None and self._hold_state in {
            AutoHoldState.HOLD,
            AutoHoldState.RELEASE,
            AutoHoldState.SAFE_HOLD,
        }:
            compensation_position = self._q_hold
            compensation_velocity = np.zeros(6, dtype=np.float64)
        else:
            compensation_position = positions
            compensation_velocity = velocities
        torque = backend.compensation_torque(
            compensation_position,
            compensation_velocity,
            self.fc,
            self.fv,
            self.vel_threshold,
            self.gravity_scale,
            self.gravity_scale_high if self.gravity_segmented else None,
            self.gravity_breakpoint if self.gravity_segmented else None,
        )
        torque = np.clip(torque + self.gravity_residual, -self.tau_limit, self.tau_limit)

        backend.write_frame(
            JointFrame(
                mode=FrameMode.POS_VEL_TQE_KP_KD,
                arm_position=cmd_positions,
                arm_velocity=cmd_velocities,
                arm_torque=torque,
                arm_kp=kp,
                arm_kd=kd,
                gripper_position=float(
                    np.clip(
                        states[6].position,
                        backend.limits.gripper_lower,
                        backend.limits.gripper_upper,
                    )
                ),
                gripper_velocity=0.0,
                gripper_torque=0.0,
                gripper_kp=0.0,
                gripper_kd=0.0,
            )
        )
        return MotionStepResult.RUNNING

    def _enter_state(self, state: AutoHoldState, now: float, label: str) -> None:
        self._hold_state = state
        self._state_since = now
        logger.info("teach auto-hold: %s", label)

    def _auto_hold_step(
        self,
        q: np.ndarray,
        v: np.ndarray,
        now: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Auto-Hold 状态机推进；返回 (kp, kd, pos_cmd, vel_cmd)。

        状态流：DRAG → STILL_DETECT → HOLD → RELEASE → DRAG。
        只依赖关节速度判定，不依赖力矩/外力估计。
        """
        cfg = self.auto_hold_cfg
        if self._hold_state is AutoHoldState.SAFE_HOLD:
            # 安全保持期间忽略新的离合命令；只由超时退出。
            clutch_request = None
        else:
            with self._lock:
                clutch_request = self._clutch_request
                self._clutch_request = None
        if clutch_request is TeachClutchCommand.LOCK:
            self._q_hold = q.copy()
            self._still_since = None
            self._hold_kp_start = self._kp_now.copy()
            self._enter_state(AutoHoldState.HOLD, now, "显式 lock -> HOLD (锁定当前位置)")
        elif clutch_request is TeachClutchCommand.DRAG:
            self._still_since = None
            if self._hold_state in {AutoHoldState.HOLD, AutoHoldState.RELEASE}:
                self._release_kp_start = self._kp_now.copy()
                self._release_kd_start = self._manual_kd_hold.copy()
                self._enter_state(AutoHoldState.RELEASE, now, "显式 drag -> RELEASE (恢复手拖)")
            else:
                self._q_hold = None
                self._kp_now.fill(0.0)
                self._enter_state(AutoHoldState.DRAG, now, "显式 drag -> DRAG")

        # 显式离合模式不再依据速度自动切换。
        if self.manual_clutch:
            if self._hold_state in {AutoHoldState.HOLD, AutoHoldState.SAFE_HOLD}:
                assert self._q_hold is not None
                elapsed = max(0.0, now - (self._state_since or now))
                s = _smoothstep(elapsed / MANUAL_CLUTCH_HOLD_RAMP_TIME_S)
                self._kp_now = self._hold_kp_start + (MANUAL_CLUTCH_KP_HOLD - self._hold_kp_start) * s
                return self._kp_now, self._manual_kd_hold, self._q_hold, np.zeros(6)
            if self._hold_state is AutoHoldState.RELEASE:
                assert self._state_since is not None
                elapsed = max(0.0, now - self._state_since)
                s = _smoothstep(elapsed / MANUAL_CLUTCH_RELEASE_RAMP_TIME_S)
                self._kp_now = self._release_kp_start * (1.0 - s)
                kd_now = self._kd_drag_now + (self._release_kd_start - self._kd_drag_now) * (1.0 - s)
                if elapsed >= MANUAL_CLUTCH_RELEASE_RAMP_TIME_S:
                    self._kp_now.fill(0.0)
                    self._q_hold = None
                    self._enter_state(AutoHoldState.DRAG, now, "显式 release -> DRAG")
                cmd = q if self._q_hold is None else self._q_hold
                return self._kp_now, kd_now, cmd, np.zeros(6)
            self._kp_now.fill(0.0)
            return self._kp_now, self._kd_drag_now, q, np.zeros(6)

        # 轻量低通速度（仅用于判定，tau 很小不引入明显延迟）
        if self._velocity_filter_updated_at is None:
            self._filtered_velocity = v.copy()
        else:
            dt = max(0.0, now - self._velocity_filter_updated_at)
            alpha = 1.0 - float(np.exp(-dt / cfg.velocity_filter_tau_s)) if dt > 0 else 1.0
            self._filtered_velocity = self._filtered_velocity + alpha * (v - self._filtered_velocity)
        self._velocity_filter_updated_at = now
        filtered = self._filtered_velocity
        all_still = bool(np.all(np.abs(filtered) < cfg.still_velocity_threshold))
        any_moving = bool(np.any(np.abs(filtered) > cfg.release_velocity_threshold))
        kd_hold = np.asarray(cfg.kd_hold, dtype=np.float64)
        kp_hold = np.asarray(cfg.kp_hold, dtype=np.float64)
        zero_vel = np.zeros(6)

        state = self._hold_state
        if state is AutoHoldState.DRAG:
            if all_still:
                self._enter_state(
                    AutoHoldState.STILL_DETECT,
                    now,
                    f"DRAG -> STILL_DETECT (|v|<{cfg.still_velocity_threshold:g} rad/s)",
                )
                self._still_since = now
            self._kp_now.fill(0.0)
            return self._kp_now, self._kd_drag_now, q, zero_vel

        if state is AutoHoldState.STILL_DETECT:
            if not all_still:
                self._enter_state(AutoHoldState.DRAG, now, "STILL_DETECT -> DRAG (速度回升)")
                self._still_since = None
                self._kp_now.fill(0.0)
                return self._kp_now, self._kd_drag_now, q, zero_vel
            if now - self._still_since >= cfg.still_duration:
                self._q_hold = q.copy()
                self._kp_now.fill(0.0)
                self._enter_state(AutoHoldState.HOLD, now, "STILL_DETECT -> HOLD (自动锁位)")
                return self._kp_now, kd_hold, self._q_hold, zero_vel
            self._kp_now.fill(0.0)
            return self._kp_now, self._kd_drag_now, q, zero_vel

        if state is AutoHoldState.HOLD:
            assert self._q_hold is not None
            elapsed = max(0.0, now - self._state_since)
            self._kp_now = kp_hold * _smoothstep(elapsed / cfg.hold_ramp_time)
            if any_moving:
                self._enter_state(AutoHoldState.RELEASE, now, "HOLD -> RELEASE (检测到重新拖动)")
            return self._kp_now, kd_hold, self._q_hold, zero_vel

        # RELEASE：kp/kd 平滑降回拖动值，完成后回 DRAG
        assert self._state_since is not None
        elapsed = max(0.0, now - self._state_since)
        s = _smoothstep(elapsed / cfg.release_ramp_time)
        self._kp_now = kp_hold * (1.0 - s)
        kd_now = self._kd_drag_now + (kd_hold - self._kd_drag_now) * (1.0 - s)
        if elapsed >= cfg.release_ramp_time:
            self._kp_now.fill(0.0)
            self._q_hold = None
            self._enter_state(AutoHoldState.DRAG, now, "RELEASE -> DRAG")
        cmd = q if self._q_hold is None else self._q_hold
        return self._kp_now, kd_now, cmd, zero_vel


class TeachPlaybackMotion:
    """非阻塞示教回放：先缓慢到起点，再按绝对时间逐帧执行。"""

    def __init__(
        self,
        *,
        frames: list[PlaybackFrame],
        mode: str,
        kp: np.ndarray,
        kd: np.ndarray,
        fc: np.ndarray,
        fv: np.ndarray,
        vel_threshold: float,
        tau_limit: np.ndarray,
        gripper_kp: float,
        gripper_kd: float,
        start_timeout_s: float = 30.0,
        settle_timeout_s: float = 2.0,
        gravity_scale: float | np.ndarray = 1.0,
        gravity_scale_high: float | np.ndarray | None = None,
        gravity_breakpoint: float | np.ndarray | None = None,
        gravity_segmented: bool = False,
        gravity_residual: float | np.ndarray = 0.0,
    ) -> None:
        if not frames:
            raise ValueError("示教回放帧不能为空")
        if mode not in {"mit", "posvel"}:
            raise ValueError("回放 mode 必须是 mit 或 posvel")
        self.frames = frames
        self.mode = mode
        self.kp = np.asarray(kp, dtype=np.float64).copy()
        self.kd = np.asarray(kd, dtype=np.float64).copy()
        self.fc = np.asarray(fc, dtype=np.float64).copy()
        self.fv = np.asarray(fv, dtype=np.float64).copy()
        self.tau_limit = np.asarray(tau_limit, dtype=np.float64).copy()
        vectors = (self.kp, self.kd, self.fc, self.fv, self.tau_limit)
        if any(value.shape != (6,) or not np.all(np.isfinite(value)) for value in vectors):
            raise ValueError("回放控制参数必须各包含 6 个有限数值")
        if np.any(self.kp < 0) or np.any(self.kd < 0) or np.any(self.tau_limit <= 0):
            raise ValueError("回放 kp/kd 不得为负，tau_limit 必须为正")
        if gripper_kp < 0 or gripper_kd < 0:
            raise ValueError("夹爪 kp/kd 不得为负")
        scale = np.asarray(gravity_scale, dtype=np.float64)
        if scale.shape == ():
            scale = np.full(6, float(scale))
        if scale.shape != (6,) or not np.all(np.isfinite(scale)) or np.any(scale <= 0):
            raise ValueError("gravity_scale 必须是正有限标量或 6 个正有限数值")
        self.vel_threshold = float(vel_threshold)
        self.gravity_scale, self.gravity_scale_high, self.gravity_breakpoint = _gravity_params(
            gravity_scale, gravity_scale_high, gravity_breakpoint
        )
        self.gravity_segmented = bool(gravity_segmented)
        residual = np.asarray(gravity_residual, dtype=np.float64)
        if residual.shape == ():
            residual = np.full(6, float(residual))
        if residual.shape != (6,) or not np.all(np.isfinite(residual)):
            raise ValueError("gravity_residual 必须是有限标量或 6 个有限数值")
        self.gravity_residual = residual.copy()
        self.gripper_kp = float(gripper_kp)
        self.gripper_kd = float(gripper_kd)
        self.start_timeout_s = start_timeout_s
        self.settle_timeout_s = settle_timeout_s
        self.reject_reason = ""
        self._fraction = 0.0
        self._phase_started_at: float | None = None
        self._playback_started_at: float | None = None
        self._cancel_reason: CancelReason | None = None
        self._deceleration_step: int | None = None
        self._last_velocity = np.zeros(6, dtype=np.float64)
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
        current = np.asarray([state.position for state in states[:6]], dtype=np.float64)
        with self._lock:
            cancel_reason = self._cancel_reason
        if cancel_reason is not None:
            return self._step_cancel(backend, current, states[6].position, cancel_reason)
        if self._phase_started_at is None:
            self._phase_started_at = now
        if self._playback_started_at is None:
            return self._step_move_to_start(backend, states, current, now)
        return self._step_playback(backend, states, current, now)

    def _step_move_to_start(
        self,
        backend: Backend,
        states,
        current: np.ndarray,
        now: float,
    ) -> MotionStepResult:
        first = self.frames[0]
        gripper_target = first.gripper_position if first.gripper_position is not None else states[6].position
        arm_reached = np.all(np.abs(first.position - current) <= 0.05)
        gripper_reached = abs(gripper_target - states[6].position) <= 0.05
        if arm_reached and gripper_reached:
            self._playback_started_at = now
            return MotionStepResult.RUNNING
        assert self._phase_started_at is not None
        if now - self._phase_started_at >= self.start_timeout_s:
            hold_current_position(backend)
            self.reject_reason = "示教回放移动到起点超时"
            return MotionStepResult.FAILED
        backend.write_frame(
            position_frame(
                backend,
                arm_position=first.position,
                arm_velocity=np.full(6, 0.5),
                gripper_position=gripper_target,
                gripper_velocity=0.5,
            )
        )
        return MotionStepResult.RUNNING

    def _step_playback(
        self,
        backend: Backend,
        states,
        current: np.ndarray,
        now: float,
    ) -> MotionStepResult:
        assert self._playback_started_at is not None
        elapsed = now - self._playback_started_at
        timestamps = [frame.timestamp_s for frame in self.frames]
        index = min(int(np.searchsorted(timestamps, elapsed, side="right")), len(self.frames) - 1)
        frame = self.frames[index]
        self._last_velocity = frame.velocity.copy()
        if elapsed <= self.frames[-1].timestamp_s:
            self._write_playback_frame(backend, states, frame)
            with self._lock:
                self._fraction = max(self._fraction, (index + 1) / len(self.frames))
            return MotionStepResult.RUNNING

        target = self.frames[-1]
        gripper_target = (
            target.gripper_position if target.gripper_position is not None else states[6].position
        )
        arm_reached = np.all(np.abs(target.position - current) <= 0.03)
        gripper_reached = abs(gripper_target - states[6].position) <= 0.03
        if arm_reached and gripper_reached:
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=target.position,
                    arm_velocity=np.full(6, POSITION_HOLD_SPEED),
                    gripper_position=gripper_target,
                )
            )
            with self._lock:
                self._fraction = 1.0
            return MotionStepResult.DONE
        if elapsed >= self.frames[-1].timestamp_s + self.settle_timeout_s:
            hold_current_position(backend)
            self.reject_reason = "示教回放末点收敛超时"
            return MotionStepResult.FAILED
        backend.write_frame(
            position_frame(
                backend,
                arm_position=target.position,
                arm_velocity=np.full(6, POSITION_HOLD_SPEED),
                gripper_position=gripper_target,
            )
        )
        return MotionStepResult.RUNNING

    def _write_playback_frame(self, backend: Backend, states, frame: PlaybackFrame) -> None:
        gripper_position = (
            frame.gripper_position if frame.gripper_position is not None else states[6].position
        )
        if self.mode == "posvel":
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=frame.position,
                    arm_velocity=np.maximum(np.abs(frame.velocity), 1e-3),
                    gripper_position=gripper_position,
                    gripper_velocity=max(abs(frame.gripper_velocity), 1e-3),
                )
            )
            return
        torque = backend.compensation_torque(
            frame.position,
            frame.velocity,
            self.fc,
            self.fv,
            self.vel_threshold,
            self.gravity_scale,
            self.gravity_scale_high if self.gravity_segmented else None,
            self.gravity_breakpoint if self.gravity_segmented else None,
        )
        torque = np.clip(torque + self.gravity_residual, -self.tau_limit, self.tau_limit)
        backend.write_frame(
            JointFrame(
                mode=FrameMode.POS_VEL_TQE_KP_KD,
                arm_position=frame.position,
                arm_velocity=frame.velocity,
                arm_torque=torque,
                arm_kp=self.kp,
                arm_kd=self.kd,
                gripper_position=gripper_position,
                gripper_velocity=frame.gripper_velocity,
                gripper_torque=0.0,
                gripper_kp=self.gripper_kp if frame.gripper_position is not None else 0.0,
                gripper_kd=self.gripper_kd if frame.gripper_position is not None else 0.3,
            )
        )

    def _step_cancel(
        self,
        backend: Backend,
        current: np.ndarray,
        gripper_position: float,
        cancel_reason: CancelReason,
    ) -> MotionStepResult:
        if self._deceleration_step is None:
            self._deceleration_step = 0
        self._deceleration_step += 1
        scale = max(0.0, 1.0 - self._deceleration_step / 12.0)
        backend.write_frame(
            position_frame(
                backend,
                arm_position=current,
                arm_velocity=np.maximum(np.abs(self._last_velocity) * scale, 1e-3),
                gripper_position=gripper_position,
            )
        )
        if self._deceleration_step < 12:
            return MotionStepResult.RUNNING
        self.reject_reason = f"示教回放已取消: {cancel_reason.value}"
        return MotionStepResult.CANCELLED
