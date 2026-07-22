"""无需真机的 Panthera-HT 一阶电机仿真后端。"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from .base import (
    BackendClosedError,
    BackendError,
    BackendLimits,
    DEFAULT_LIMITS,
    DISCONNECTED_SENTINEL,
    FrameMode,
    JointFrame,
    LimitViolationError,
    MotorSnapshot,
    filter_idle_velocity,
    passive_idle_frame,
    smooth_idle_damping_frame,
)


class SimBackend:
    """模拟 6 关节 + 1 夹爪的共享整帧控制语义。

    仿真刻意不启动后台线程。状态只在 HardwareLoop 调用 `refresh_state()`、
    下发新帧或执行安全动作时按单调时钟推进，因此测试可注入假时钟并完全确定。
    """

    n_joints = 6
    is_sim = True
    sdk_version = "sim"
    estop_latch_hazard_present = False

    def __init__(
        self,
        *,
        limits: BackendLimits = DEFAULT_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(limits.joint_lower) != self.n_joints:
            raise ValueError(f"仿真后端要求 {self.n_joints} 个关节限位")
        self.limits = limits
        self._clock = clock
        self._positions = np.zeros(7, dtype=np.float64)
        self._velocities = np.zeros(7, dtype=np.float64)
        self._torques = np.zeros(7, dtype=np.float64)
        self._target_positions = self._positions.copy()
        self._target_velocities = self._velocities.copy()
        self._max_torque = np.concatenate((limits.joint_torque, [limits.gripper_torque]))
        self._feedforward_torque = np.zeros(7, dtype=np.float64)
        self._kp = np.zeros(7, dtype=np.float64)
        self._kd = np.zeros(7, dtype=np.float64)
        self._position_lower = np.concatenate((limits.joint_lower, [limits.gripper_lower]))
        self._position_upper = np.concatenate((limits.joint_upper, [limits.gripper_upper]))
        self._velocity_limit = np.concatenate((limits.joint_velocity, [limits.gripper_velocity]))
        self._connected = np.ones(7, dtype=np.bool_)
        self._faults = np.zeros(7, dtype=np.int64)
        self._pos_limit_flags = np.zeros(7, dtype=np.int8)
        self._tor_limit_flags = np.zeros(7, dtype=np.int8)
        self._mode = FrameMode.STOP
        self._last_frame: JointFrame | None = None
        self._idle_mode: str | None = "damping"
        self._idle_filtered_velocity = np.zeros(6, dtype=np.float64)
        self._idle_filter_updated_at = self._clock()
        self._last_update = self._clock()
        self._motor_time = self._last_update
        self._closed = False

    def refresh_state(self) -> None:
        self._require_open()
        self._advance()

    def read_all(self) -> list[MotorSnapshot]:
        self._require_open()
        snapshots: list[MotorSnapshot] = []
        for index in range(7):
            position = self._positions[index] if self._connected[index] else DISCONNECTED_SENTINEL
            snapshots.append(
                MotorSnapshot(
                    name=f"joint{index + 1}",
                    motor_id=index + 1,
                    position=float(position),
                    velocity=float(self._velocities[index]) if self._connected[index] else 0.0,
                    torque=float(self._torques[index]) if self._connected[index] else 0.0,
                    motor_time=self._motor_time if self._connected[index] else 0.0,
                    mode=int(self._mode),
                    fault=int(self._faults[index]),
                    pos_limit_flag=int(self._pos_limit_flags[index]),
                    tor_limit_flag=int(self._tor_limit_flags[index]),
                )
            )
        return snapshots

    def write_frame(self, frame: JointFrame) -> None:
        self._require_open()
        frame.validate(self.n_joints)
        self._idle_mode = None
        self._advance()
        if frame.mode in {FrameMode.STOP, FrameMode.BRAKE}:
            self._freeze(frame.mode)
            self._last_frame = None
            return

        positions = np.concatenate((frame.arm_position, [frame.gripper_position]))
        velocities = np.concatenate((frame.arm_velocity, [frame.gripper_velocity]))
        if frame.mode in {FrameMode.POS_VEL_TQE, FrameMode.POS_VEL_TQE_KP_KD}:
            self._validate_position_targets(positions)

        self._mode = frame.mode
        self._target_positions = positions
        self._target_velocities = velocities
        self._pos_limit_flags.fill(0)
        self._tor_limit_flags.fill(0)

        if frame.mode is FrameMode.POS_VEL_TQE:
            assert frame.arm_max_torque is not None
            self._max_torque = np.concatenate((frame.arm_max_torque, [frame.gripper_max_torque]))
            self._feedforward_torque.fill(0.0)
            self._kp.fill(0.0)
            self._kd.fill(0.0)
        elif frame.mode is FrameMode.POS_VEL_TQE_KP_KD:
            assert frame.arm_torque is not None
            assert frame.arm_kp is not None
            assert frame.arm_kd is not None
            self._feedforward_torque = np.concatenate((frame.arm_torque, [frame.gripper_torque]))
            self._kp = np.concatenate((frame.arm_kp, [frame.gripper_kp]))
            self._kd = np.concatenate((frame.arm_kd, [frame.gripper_kd]))
        self._last_frame = frame

    def compensation_torque(
        self,
        q: np.ndarray,
        v: np.ndarray,
        fc: np.ndarray,
        fv: np.ndarray,
        vel_threshold: float,
    ) -> np.ndarray:
        """仿真模型没有重力项，只复现 SDK 的摩擦补偿公式。"""
        self._require_open()
        positions = np.asarray(q, dtype=np.float64)
        velocities = np.asarray(v, dtype=np.float64)
        coulomb = np.asarray(fc, dtype=np.float64)
        viscous = np.asarray(fv, dtype=np.float64)
        if any(value.shape != (6,) for value in (positions, velocities, coulomb, viscous)):
            raise ValueError("补偿向量长度必须为 6")
        if vel_threshold < 0 or not np.isfinite(vel_threshold):
            raise ValueError("vel_threshold 必须是非负有限数值")
        full = coulomb * np.sign(velocities) + viscous * velocities
        low_speed = viscous * velocities
        return np.where(np.abs(velocities) < vel_threshold, low_speed, full)

    def maintain_idle(self) -> None:
        self._require_open()
        idle_mode = self._idle_mode
        if idle_mode is None:
            return

        if idle_mode == "damping":
            now = self._clock()
            self._idle_filtered_velocity = filter_idle_velocity(
                self._idle_filtered_velocity,
                self._velocities[:6],
                dt_s=max(0.0, now - self._idle_filter_updated_at),
            )
            self._idle_filter_updated_at = now
            frame = smooth_idle_damping_frame(
                self.limits,
                self._positions[:6],
                self._idle_filtered_velocity,
                self._positions[6],
            )
        else:
            frame = passive_idle_frame(self.limits, self._positions[:6], self._positions[6])
        self.write_frame(frame)
        self._idle_mode = idle_mode

    def enter_idle_damping(self) -> None:
        self._require_open()
        self._last_frame = None
        self._idle_mode = "damping"
        self._idle_filtered_velocity.fill(0.0)
        self._idle_filter_updated_at = self._clock()

    def enter_passive_idle(self) -> None:
        self._require_open()
        self._last_frame = None
        self._idle_mode = "passive"
        self._idle_filtered_velocity.fill(0.0)
        self._idle_filter_updated_at = self._clock()

    def stop(self) -> None:
        self._require_open()
        self._advance()
        self._freeze(FrameMode.STOP)
        self._idle_mode = None

    def set_zero(self, motor_ids: list[int] | None = None) -> tuple[bool, bool, str]:
        self._require_open()
        self._advance()
        ids = list(range(1, 8)) if motor_ids is None else list(dict.fromkeys(motor_ids))
        if not ids:
            return False, False, "motor_ids 不能为空；省略该字段表示全部电机"
        invalid = [motor_id for motor_id in ids if motor_id < 1 or motor_id > 7]
        if invalid:
            return False, False, f"未知电机 ID: {invalid}"
        disconnected = [motor_id for motor_id in ids if not self._connected[motor_id - 1]]
        if disconnected:
            return False, False, f"电机未连接: {disconnected}"

        indices = np.array(ids, dtype=np.int64) - 1
        self._positions[indices] = 0.0
        self._target_positions[indices] = 0.0
        self._velocities[indices] = 0.0
        self._target_velocities[indices] = 0.0
        self._torques[indices] = 0.0
        self._pos_limit_flags[indices] = 0
        self._last_frame = None
        persisted = motor_ids is not None
        return True, persisted, ""

    def set_motor_connected(self, motor_id: int, connected: bool) -> None:
        """仿真故障注入：控制某个电机是否返回 999.0 未连接哨兵。"""
        self._require_open()
        if motor_id < 1 or motor_id > 7:
            raise ValueError(f"未知电机 ID: {motor_id}")
        self._connected[motor_id - 1] = connected

    def set_motor_fault(self, motor_id: int, fault: int) -> None:
        """仿真故障注入：设置与固件透传一致的 fault 字段。"""
        self._require_open()
        if motor_id < 1 or motor_id > 7:
            raise ValueError(f"未知电机 ID: {motor_id}")
        if fault < 0 or fault > 0xFF:
            raise ValueError("fault 必须位于 0..255")
        self._faults[motor_id - 1] = fault

    def close(self) -> None:
        if self._closed:
            return
        self._advance()
        self._freeze(FrameMode.STOP)
        self._last_frame = None
        self._closed = True

    def _advance(self) -> None:
        now = self._clock()
        dt = now - self._last_update
        if dt < 0:
            raise BackendError("单调时钟发生倒退，无法推进仿真")
        self._last_update = now
        self._motor_time = now
        if dt == 0:
            return

        if self._mode is FrameMode.VELOCITY:
            velocity = np.clip(self._target_velocities, -self._velocity_limit, self._velocity_limit)
            self._integrate_velocity(velocity, dt)
            self._torques.fill(0.0)
        elif self._mode is FrameMode.POS_VEL_TQE:
            speed = np.minimum(np.abs(self._target_velocities), self._velocity_limit)
            self._approach_targets(speed, dt)
            error = self._target_positions - self._positions
            self._torques = np.clip(error * 5.0, -self._max_torque, self._max_torque)
        elif self._mode is FrameMode.POS_VEL_TQE_KP_KD:
            position_error = self._target_positions - self._positions
            desired_velocity = self._target_velocities + 0.05 * self._kp * position_error
            desired_velocity += 0.01 * self._feedforward_torque
            velocity = np.clip(desired_velocity, -self._velocity_limit, self._velocity_limit)
            self._integrate_velocity(velocity, dt)
            torque = self._feedforward_torque + self._kp * position_error - self._kd * self._velocities
            self._torques = np.clip(torque, -self._max_torque, self._max_torque)
        else:
            self._velocities.fill(0.0)
            self._torques.fill(0.0)

    def _approach_targets(self, speed: np.ndarray, dt: float) -> None:
        error = self._target_positions - self._positions
        step = np.clip(error, -speed * dt, speed * dt)
        self._positions += step
        self._velocities = step / dt
        reached = np.isclose(self._positions, self._target_positions, atol=1e-12)
        self._velocities[reached] = 0.0

    def _integrate_velocity(self, velocity: np.ndarray, dt: float) -> None:
        proposed = self._positions + velocity * dt
        clipped = np.clip(proposed, self._position_lower, self._position_upper)
        hit_upper = proposed > self._position_upper
        hit_lower = proposed < self._position_lower
        self._pos_limit_flags = hit_upper.astype(np.int8) - hit_lower.astype(np.int8)
        self._positions = clipped
        self._velocities = velocity.copy()
        self._velocities[hit_upper | hit_lower] = 0.0

    def _validate_position_targets(self, positions: np.ndarray) -> None:
        below = positions < self._position_lower
        above = positions > self._position_upper
        if not np.any(below | above):
            return
        index = int(np.flatnonzero(below | above)[0])
        name = f"joint{index + 1}" if index < 6 else "gripper"
        direction = "下限" if below[index] else "上限"
        limit = self._position_lower[index] if below[index] else self._position_upper[index]
        raise LimitViolationError(f"{name} 目标 {positions[index]:.6g} 超过{direction} {limit:.6g}")

    def _freeze(self, mode: FrameMode) -> None:
        self._mode = mode
        self._target_positions = self._positions.copy()
        self._target_velocities.fill(0.0)
        self._velocities.fill(0.0)
        self._torques.fill(0.0)
        self._feedforward_torque.fill(0.0)

    def _require_open(self) -> None:
        if self._closed:
            raise BackendClosedError("仿真后端已关闭")
