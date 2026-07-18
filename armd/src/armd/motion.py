"""M3 非阻塞关节运动状态机。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np

from .backend import Backend, BackendError, FrameMode, JointFrame
from .hardware_loop import CancelReason, MotionStepResult

POSITION_HOLD_SPEED = 0.1
JOG_FRESHNESS_S = 0.25
JOG_LIMIT_MARGIN = 0.02


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
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE,
        arm_position=arm_position,
        arm_velocity=arm_velocity,
        arm_max_torque=backend.limits.joint_torque if arm_max_torque is None else arm_max_torque,
        gripper_position=gripper_position,
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
            velocities.fill(0.0)
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
        return MotionStepResult.CANCELLED if cancel_reason is not None else MotionStepResult.RUNNING
