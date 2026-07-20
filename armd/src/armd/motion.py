"""M3 非阻塞关节运动状态机。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np

from .backend import (
    IDLE_DAMPING_KD,
    Backend,
    BackendError,
    FrameMode,
    JointFrame,
    idle_damping_frame,
)
from .hardware_loop import CancelReason, MotionStepResult
from .teach import PlaybackFrame

POSITION_HOLD_SPEED = 0.1
JOG_FRESHNESS_S = 0.25
JOG_LIMIT_MARGIN = 0.02
MIT_FRESHNESS_S = 0.12
TEACH_VEL_THRESHOLD_S = 0.02
TEACH_TAU_LIMIT = np.array([15.0, 30.0, 30.0, 15.0, 5.0, 5.0], dtype=np.float64)
GRIPPER_POSITION_TORQUE_FRACTION = 0.8
GRIPPER_POSITION_MAX_KP = 5.0
GRIPPER_POSITION_MAX_KD = 0.5


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
    回放默认量级。这样既不会为了移动夹爪把六个关节切进持续刚性位置保持，
    也不会因按整个夹爪行程分摊力矩而让短距离动作失去驱动力。
    """
    limits = backend.limits
    safe_position = float(
        np.clip(gripper_position, limits.gripper_lower, limits.gripper_upper)
    )
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
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE_KP_KD,
        arm_position=np.clip(arm_position, limits.joint_lower, limits.joint_upper),
        arm_velocity=np.zeros(6),
        arm_torque=np.zeros(6),
        arm_kp=np.zeros(6),
        arm_kd=IDLE_DAMPING_KD,
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
        if cancel_reason is not None:
            backend.write_frame(
                idle_damping_frame(backend.limits, arm_position, states[6].position)
            )
            self.reject_reason = f"夹爪运动已取消: {cancel_reason.value}"
            return MotionStepResult.CANCELLED

        error = self.position - states[6].position
        if abs(error) <= self.tolerance:
            backend.write_frame(
                idle_damping_frame(backend.limits, arm_position, states[6].position)
            )
            return MotionStepResult.DONE
        if (
            self._started_at is not None
            and self.timeout_s is not None
            and now - self._started_at >= self.timeout_s
        ):
            backend.write_frame(
                idle_damping_frame(backend.limits, arm_position, states[6].position)
            )
            self.reject_reason = "夹爪运动超时"
            return MotionStepResult.FAILED

        backend.write_frame(
            gripper_position_frame(
                backend,
                arm_position=arm_position,
                gripper_position=self.position,
                gripper_current_position=states[6].position,
                gripper_current_velocity=states[6].velocity,
                gripper_velocity=self.velocity,
                gripper_max_torque=self.max_torque,
            )
        )
        return MotionStepResult.RUNNING


class JointPositionMotion:
    """逐周期重发 POS-VEL 目标，并在到达/超时/取消时安全收尾。"""

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
        if np.all(self.errors <= self.tolerance):
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=self.positions,
                    arm_velocity=np.full(6, POSITION_HOLD_SPEED),
                    arm_max_torque=self.max_torque,
                    gripper_position=states[6].position,
                )
            )
            return MotionStepResult.DONE
        if now >= self.deadline:
            hold_current_position(backend)
            self.reject_reason = "等待关节到位超时"
            return MotionStepResult.FAILED

        backend.write_frame(
            position_frame(
                backend,
                arm_position=self.positions,
                arm_velocity=self.velocities,
                arm_max_torque=self.max_torque,
                gripper_position=states[6].position,
            )
        )
        return MotionStepResult.RUNNING


class JointJogMotion:
    """流式关节速度控制；超过 250ms 无新指令即整帧速度归零。"""

    def __init__(
        self,
        *,
        freshness_s: float = JOG_FRESHNESS_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.freshness_s = freshness_s
        self._clock = clock
        self._velocities = np.zeros(6, dtype=np.float64)
        self._last_command_at = float("-inf")
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
            velocities = self._velocities.copy()
            stale = now - self._last_command_at > self.freshness_s
        if cancel_reason is not None:
            positions = np.array([state.position for state in states[:6]], dtype=np.float64)
            backend.write_frame(
                idle_damping_frame(backend.limits, positions, states[6].position)
            )
            return MotionStepResult.CANCELLED
        elif stale:
            velocities.fill(0.0)

        positions = np.array([state.position for state in states[:6]], dtype=np.float64)
        at_upper = positions >= backend.limits.joint_upper - JOG_LIMIT_MARGIN
        at_lower = positions <= backend.limits.joint_lower + JOG_LIMIT_MARGIN
        limit_hit = (at_upper & (velocities > 0)) | (at_lower & (velocities < 0))
        velocities[limit_hit] = 0.0
        if np.any(np.abs(velocities) > backend.limits.joint_velocity):
            raise BackendError("JointJog 速度超过软限位")

        with self._lock:
            self._limit_hit = limit_hit
        backend.write_frame(
            JointFrame(
                mode=FrameMode.VELOCITY,
                arm_position=positions,
                arm_velocity=velocities,
                gripper_position=states[6].position,
                gripper_velocity=0.0,
            )
        )
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
        self._last_index = 0
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
        index = min(int(np.searchsorted(self.timestamps, elapsed, side="right")), len(self.positions) - 1)
        self._last_index = index
        if elapsed < self.timestamps[-1]:
            speed = np.maximum(np.abs(self.velocities[index]), 1e-3)
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=self.positions[index],
                    arm_velocity=speed,
                    arm_max_torque=self.max_torque,
                    gripper_position=states[6].position,
                )
            )
            with self._lock:
                self._fraction = max(self._fraction, (index + 1) / len(self.positions))
            return MotionStepResult.RUNNING

        target = self.positions[-1]
        self.errors = np.abs(target - current)
        if np.all(self.errors <= self.tolerance):
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=target,
                    arm_velocity=np.full(6, POSITION_HOLD_SPEED),
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
        backend.write_frame(
            position_frame(
                backend,
                arm_position=target,
                arm_velocity=np.full(6, POSITION_HOLD_SPEED),
                arm_max_torque=self.max_torque,
                gripper_position=states[6].position,
            )
        )
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
    """重力/摩擦前馈的连续拖动示教模式。"""

    def __init__(
        self,
        *,
        kp: np.ndarray,
        kd: np.ndarray,
        fc: np.ndarray,
        fv: np.ndarray,
        tau_limit: np.ndarray = TEACH_TAU_LIMIT,
        vel_threshold: float = TEACH_VEL_THRESHOLD_S,
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
        self.vel_threshold = float(vel_threshold)
        self.reject_reason = ""
        self._cancel_reason: CancelReason | None = None
        self._lock = threading.Lock()

    @property
    def fraction(self) -> float:
        return 0.0

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        del now
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
            backend.write_frame(idle_damping_frame(backend.limits, positions, states[6].position))
            self.reject_reason = f"示教已停止: {cancel_reason.value}"
            return MotionStepResult.CANCELLED

        torque = backend.compensation_torque(
            positions,
            velocities,
            self.fc,
            self.fv,
            self.vel_threshold,
        )
        torque = np.clip(torque, -self.tau_limit, self.tau_limit)
        backend.write_frame(
            JointFrame(
                mode=FrameMode.POS_VEL_TQE_KP_KD,
                arm_position=positions,
                arm_velocity=np.zeros(6),
                arm_torque=torque,
                arm_kp=self.kp,
                arm_kd=self.kd,
                gripper_position=states[6].position,
                gripper_velocity=0.0,
                gripper_torque=0.0,
                gripper_kp=0.0,
                gripper_kd=0.0,
            )
        )
        return MotionStepResult.RUNNING


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
        self.vel_threshold = float(vel_threshold)
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
        )
        torque = np.clip(torque, -self.tau_limit, self.tau_limit)
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
