"""M3 非阻塞关节运动状态机。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np

from .backend import Backend, BackendError, FrameMode, JointFrame, idle_damping_frame
from .hardware_loop import CancelReason, MotionStepResult

POSITION_HOLD_SPEED = 0.1
JOG_FRESHNESS_S = 0.25
JOG_LIMIT_MARGIN = 0.02
MIT_FRESHNESS_S = 0.12


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
            hold_current_position(backend)
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
        tolerance: float = 0.01,
        settle_timeout_s: float = 2.0,
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
            self.reject_reason = "moveL 末点收敛超时"
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
