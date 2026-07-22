"""官方 Panthera SDK 的真实硬件后端。"""

from __future__ import annotations

import importlib
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

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

EXPECTED_MOTOR_COUNT = 7
MIN_SAFE_STATE_QUERY_FIRMWARE = (4, 2, 0)
DEFAULT_MOTOR_TIMEOUT_MS = 150


class SdkAuditError(BackendError):
    """SDK 源码或固件不满足已核实的安全前提。"""


@dataclass(frozen=True, slots=True)
class SdkAuditResult:
    sdk_root: Path
    detect_motor_limit_occurrences: tuple[str, ...]
    detect_motor_limit_call_sites: tuple[str, ...]

    @property
    def estop_latch_hazard_present(self) -> bool:
        return bool(self.detect_motor_limit_call_sites)


def audit_sdk_source(sdk_root: str | Path) -> SdkAuditResult:
    """确认 ``detect_motor_limit()`` 仍只有声明和定义，没有进入调用链。"""

    root = Path(sdk_root).expanduser().resolve()
    source_root = root / "panthera_cpp" / "motor_cpp"
    if not source_root.is_dir():
        raise SdkAuditError(f"SDK 源码目录不存在: {source_root}")

    occurrences: list[str] = []
    call_sites: list[str] = []
    for path in sorted(source_root.rglob("*")):
        if path.suffix not in {".cpp", ".cc", ".cxx", ".h", ".hpp"} or "third_part" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(
            r"/\*.*?\*/",
            lambda match: "\n" * match.group(0).count("\n"),
            text,
            flags=re.DOTALL,
        )
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.split("//", 1)[0].strip()
            if "detect_motor_limit" not in line:
                continue
            location = f"{path.relative_to(root)}:{line_number}"
            occurrences.append(location)
            if re.search(r"\bvoid\s+(?:robot::)?detect_motor_limit\s*\(", line):
                continue
            call_sites.append(location)

    if len(occurrences) < 2:
        raise SdkAuditError("SDK 源码中未找到 detect_motor_limit() 的完整声明/定义，无法验证 EStop 前提")
    return SdkAuditResult(root, tuple(occurrences), tuple(call_sites))


class RealBackend:
    """封装官方 ``Panthera`` 对象，并强制整帧同模式下发。"""

    n_joints = 6
    is_sim = False

    def __init__(
        self,
        *,
        sdk_root: str | Path,
        config_path: str | Path | None = None,
        motor_timeout_ms: int = DEFAULT_MOTOR_TIMEOUT_MS,
        limits: BackendLimits | None = None,
        robot_factory: Callable[[str | None], Any] | None = None,
        sdk_module: ModuleType | Any | None = None,
        source_audit: SdkAuditResult | None = None,
        run_source_audit: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= motor_timeout_ms <= 32760:
            raise ValueError("motor_timeout_ms 必须位于 0..32760")

        self._closed = False
        self._robot: Any | None = None
        self._motors: tuple[Any, ...] = ()
        self._motor_firmware_versions: tuple[str, ...] = ()
        self._state_query_safe = False
        self._clock = clock
        self._next_version_check_at = 0.0
        self._last_frame: JointFrame | None = None
        self._idle_mode: str | None = "damping"
        self._idle_filtered_velocity = np.zeros(6, dtype=np.float64)
        self._idle_filter_updated_at = self._clock()
        self.motor_timeout_ms = motor_timeout_ms

        if source_audit is None and run_source_audit:
            source_audit = audit_sdk_source(sdk_root)
        self.source_audit = source_audit
        if source_audit is not None and source_audit.estop_latch_hazard_present:
            sites = ", ".join(source_audit.detect_motor_limit_call_sites)
            raise SdkAuditError(f"detect_motor_limit() 已进入调用链，EStop 可靠性前提失效: {sites}")
        self.estop_latch_hazard_present = False

        if robot_factory is None:
            panthera_class, sdk_module = _load_sdk(sdk_root)

            def create_robot(path: str | None) -> Any:
                return panthera_class(path) if path is not None else panthera_class()

            robot_factory = create_robot

        config = str(Path(config_path).expanduser().resolve()) if config_path is not None else None
        try:
            self._robot = robot_factory(config)
            self._replace_motor_handles(force_version_check=True)
            self.limits = limits or _limits_from_robot(self._robot)
            if motor_timeout_ms > 0:
                self._robot.set_timeout(motor_timeout_ms)
        except BaseException:
            self._closed = True
            self._motors = ()
            self._robot = None
            raise

        python_version = str(getattr(sdk_module, "__version__", "unknown"))
        cpp_version = str(getattr(sdk_module, "__cpp_sdk_version__", "unknown"))
        self._sdk_base_version = f"python={python_version},cpp={cpp_version}"
        self.sdk_version = self._format_sdk_version()

    @property
    def motor_firmware_versions(self) -> tuple[str, ...]:
        return self._motor_firmware_versions

    def refresh_state(self) -> None:
        self._require_open()
        self._replace_motor_handles()
        if not self._state_query_safe:
            return
        robot = self._require_robot()
        robot.send_get_motor_state_cmd()
        robot.motor_send_cmd()

    def read_all(self) -> list[MotorSnapshot]:
        self._require_open()
        if len(self._motors) != EXPECTED_MOTOR_COUNT or not self._state_query_safe:
            return _disconnected_snapshots()

        snapshots: list[MotorSnapshot] = []
        for index, motor in enumerate(self._motors):
            state = motor.get_current_motor_state()
            snapshots.append(
                MotorSnapshot(
                    name=str(motor.get_motor_name()),
                    motor_id=int(state.ID),
                    position=float(state.position),
                    velocity=float(state.velocity),
                    torque=float(state.torque),
                    motor_time=float(state.time),
                    mode=int(state.mode),
                    fault=int(state.fault),
                    pos_limit_flag=int(motor.pos_limit_flag),
                    tor_limit_flag=int(motor.tor_limit_flag),
                )
            )
        return snapshots

    def write_frame(self, frame: JointFrame) -> None:
        self._require_open()
        frame.validate(self.n_joints)
        self._validate_frame_limits(frame)
        self._idle_mode = None
        robot = self._require_robot()

        if frame.mode is FrameMode.STOP:
            robot.set_stop()
            return

        motors = self._require_motors()
        if frame.mode is FrameMode.BRAKE:
            for motor in motors:
                motor.brake()
        elif frame.mode is FrameMode.VELOCITY:
            velocities = np.concatenate((frame.arm_velocity, [frame.gripper_velocity]))
            for motor, velocity in zip(motors, velocities, strict=True):
                motor.velocity(float(velocity))
        elif frame.mode is FrameMode.POS_VEL_TQE:
            assert frame.arm_max_torque is not None
            positions = np.concatenate((frame.arm_position, [frame.gripper_position]))
            velocities = np.concatenate((frame.arm_velocity, [frame.gripper_velocity]))
            max_torques = np.concatenate((frame.arm_max_torque, [frame.gripper_max_torque]))
            for motor, position, velocity, max_torque in zip(
                motors, positions, velocities, max_torques, strict=True
            ):
                motor.pos_vel_MAXtqe(float(position), float(velocity), float(max_torque))
        elif frame.mode is FrameMode.POS_VEL_TQE_KP_KD:
            assert frame.arm_torque is not None
            assert frame.arm_kp is not None
            assert frame.arm_kd is not None
            positions = np.concatenate((frame.arm_position, [frame.gripper_position]))
            velocities = np.concatenate((frame.arm_velocity, [frame.gripper_velocity]))
            torques = np.concatenate((frame.arm_torque, [frame.gripper_torque]))
            kps = np.concatenate((frame.arm_kp, [frame.gripper_kp]))
            kds = np.concatenate((frame.arm_kd, [frame.gripper_kd]))
            for motor, position, velocity, torque, kp, kd in zip(
                motors, positions, velocities, torques, kps, kds, strict=True
            ):
                motor.pos_vel_tqe_kp_kd(
                    float(position),
                    float(velocity),
                    float(torque),
                    float(kp),
                    float(kd),
                )
        else:
            raise ValueError(f"不支持的真实控制模式: {frame.mode.name}")

        robot.motor_send_cmd()
        self._last_frame = frame

    def compensation_torque(
        self,
        q: np.ndarray,
        v: np.ndarray,
        fc: np.ndarray,
        fv: np.ndarray,
        vel_threshold: float,
    ) -> np.ndarray:
        """在硬件线程内调用官方 SDK 的重力与摩擦补偿实现。"""
        self._require_open()
        positions = np.asarray(q, dtype=np.float64)
        velocities = np.asarray(v, dtype=np.float64)
        coulomb = np.asarray(fc, dtype=np.float64)
        viscous = np.asarray(fv, dtype=np.float64)
        if any(value.shape != (6,) for value in (positions, velocities, coulomb, viscous)):
            raise ValueError("补偿向量长度必须为 6")
        if vel_threshold < 0 or not np.isfinite(vel_threshold):
            raise ValueError("vel_threshold 必须是非负有限数值")
        robot = self._require_robot()
        gravity = np.asarray(robot.get_Gravity(positions), dtype=np.float64)
        friction = np.asarray(
            robot.get_friction_compensation(
                velocities,
                coulomb,
                viscous,
                vel_threshold,
            ),
            dtype=np.float64,
        )
        if gravity.shape != (6,) or friction.shape != (6,):
            raise BackendError("SDK 补偿力矩返回长度不是 6")
        return gravity + friction

    def maintain_idle(self) -> None:
        self._require_open()
        idle_mode = self._idle_mode
        if idle_mode is None:
            return
        states = self.read_all()
        if len(states) != EXPECTED_MOTOR_COUNT or not all(state.valid for state in states):
            self.stop()
            return

        positions = np.array([state.position for state in states[:6]], dtype=np.float64)
        if idle_mode == "damping":
            now = self._clock()
            self._idle_filtered_velocity = filter_idle_velocity(
                self._idle_filtered_velocity,
                np.array([state.velocity for state in states[:6]], dtype=np.float64),
                dt_s=max(0.0, now - self._idle_filter_updated_at),
            )
            self._idle_filter_updated_at = now
            frame = smooth_idle_damping_frame(
                self.limits,
                positions,
                self._idle_filtered_velocity,
                states[6].position,
            )
        else:
            frame = passive_idle_frame(self.limits, positions, states[6].position)
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
        self._require_robot().set_stop()
        self._last_frame = None
        self._idle_mode = None

    def set_zero(self, motor_ids: list[int] | None = None) -> tuple[bool, bool, str]:
        self._require_open()
        motors = self._require_motors()
        ids = list(range(1, 8)) if motor_ids is None else list(dict.fromkeys(motor_ids))
        if not ids:
            return False, False, "motor_ids 不能为空；省略该字段表示全部电机"
        invalid = [motor_id for motor_id in ids if motor_id < 1 or motor_id > 7]
        if invalid:
            return False, False, f"未知电机 ID: {invalid}"

        states = [motor.get_current_motor_state() for motor in motors]
        disconnected = [
            motor_id for motor_id in ids if float(states[motor_id - 1].position) == DISCONNECTED_SENTINEL
        ]
        if disconnected:
            return False, False, f"电机未连接: {disconnected}"

        robot = self._require_robot()
        if motor_ids is None:
            robot.set_reset_zero()
            robot.motor_send_cmd()
            persisted = False
        else:
            robot.set_reset_zero_motors([motor_id - 1 for motor_id in ids])
            persisted = True
        self._last_frame = None
        self._replace_motor_handles(force_version_check=True)
        return True, persisted, ""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._motors = ()
        self._robot = None

    def _replace_motor_handles(self, *, force_version_check: bool = False) -> None:
        robot = self._require_robot()
        motors = tuple(robot.get_motors())
        if len(motors) != EXPECTED_MOTOR_COUNT:
            self._motors = ()
            self._motor_firmware_versions = ()
            self._state_query_safe = False
            self._next_version_check_at = 0.0
            if hasattr(self, "_sdk_base_version"):
                self.sdk_version = self._format_sdk_version()
            return

        now = self._clock()
        if not force_version_check and self._state_query_safe and now < self._next_version_check_at:
            self._motors = motors
            return

        versions = tuple(_motor_version(motor) for motor in motors)
        known_versions = [version for version in versions if version != (0, 0, 0)]
        if known_versions and min(known_versions) < MIN_SAFE_STATE_QUERY_FIRMWARE:
            version = ".".join(str(part) for part in min(known_versions))
            raise SdkAuditError(f"电机固件最低版本 {version} < 4.2.0；状态查询可能退化为 velocity(0) 写操作")

        self._motors = motors
        self._state_query_safe = len(known_versions) == EXPECTED_MOTOR_COUNT
        self._motor_firmware_versions = tuple(".".join(str(part) for part in version) for version in versions)
        self._next_version_check_at = now + 1.0
        if hasattr(self, "_sdk_base_version"):
            self.sdk_version = self._format_sdk_version()

    def _require_motors(self) -> tuple[Any, ...]:
        self._replace_motor_handles()
        if len(self._motors) != EXPECTED_MOTOR_COUNT:
            raise BackendError("真实后端当前未获得完整的 7 电机句柄，可能正在串口重连")
        if not self._state_query_safe:
            raise SdkAuditError("尚未读取到全部电机固件版本，拒绝下发控制帧")
        return self._motors

    def _validate_frame_limits(self, frame: JointFrame) -> None:
        if frame.mode in {FrameMode.POS_VEL_TQE, FrameMode.POS_VEL_TQE_KP_KD}:
            positions = np.concatenate((frame.arm_position, [frame.gripper_position]))
            lower = np.concatenate((self.limits.joint_lower, [self.limits.gripper_lower]))
            upper = np.concatenate((self.limits.joint_upper, [self.limits.gripper_upper]))
            _raise_limit_violation(positions, lower, upper, "目标位置")

        velocity_limits = np.concatenate((self.limits.joint_velocity, [self.limits.gripper_velocity]))
        velocities = np.concatenate((frame.arm_velocity, [frame.gripper_velocity]))
        _raise_magnitude_violation(velocities, velocity_limits, "速度")

        torque_limits = np.concatenate((self.limits.joint_torque, [self.limits.gripper_torque]))
        if frame.mode is FrameMode.POS_VEL_TQE:
            assert frame.arm_max_torque is not None
            torques = np.concatenate((frame.arm_max_torque, [frame.gripper_max_torque]))
            _raise_magnitude_violation(torques, torque_limits, "最大力矩")
        elif frame.mode is FrameMode.POS_VEL_TQE_KP_KD:
            assert frame.arm_torque is not None
            torques = np.concatenate((frame.arm_torque, [frame.gripper_torque]))
            _raise_magnitude_violation(torques, torque_limits, "前馈力矩")

    def _format_sdk_version(self) -> str:
        firmware = ",".join(self._motor_firmware_versions) if self._motor_firmware_versions else "unavailable"
        return f"{self._sdk_base_version},motors={firmware}"

    def _require_robot(self) -> Any:
        if self._robot is None:
            raise BackendClosedError("真实后端已关闭")
        return self._robot

    def _require_open(self) -> None:
        if self._closed:
            raise BackendClosedError("真实后端已关闭")


def _load_sdk(sdk_root: str | Path) -> tuple[type[Any], ModuleType]:
    root = Path(sdk_root).expanduser().resolve()
    scripts_root = root / "panthera_python" / "scripts"
    if not (scripts_root / "Panthera_lib" / "Panthera.py").is_file():
        raise SdkAuditError(f"未找到 Panthera Python SDK: {scripts_root}")
    scripts_path = str(scripts_root)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    module = importlib.import_module("Panthera_lib.Panthera")
    sdk_module = importlib.import_module("hightorque_robot")
    return module.Panthera, sdk_module


def _limits_from_robot(robot: Any) -> BackendLimits:
    try:
        joint_limits = robot.joint_limits
        gripper_limits = robot.gripper_limits
        return BackendLimits(
            joint_lower=np.asarray(joint_limits["lower"], dtype=np.float64),
            joint_upper=np.asarray(joint_limits["upper"], dtype=np.float64),
            joint_velocity=np.asarray(robot.velocity_limits, dtype=np.float64),
            joint_torque=np.asarray(robot.max_torque, dtype=np.float64),
            gripper_lower=float(gripper_limits["lower"]),
            gripper_upper=float(gripper_limits["upper"]),
            gripper_velocity=DEFAULT_LIMITS.gripper_velocity,
            gripper_torque=DEFAULT_LIMITS.gripper_torque,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return DEFAULT_LIMITS


def _motor_version(motor: Any) -> tuple[int, int, int]:
    version = motor.get_version()
    return int(version.major), int(version.minor), int(version.patch)


def _disconnected_snapshots() -> list[MotorSnapshot]:
    return [
        MotorSnapshot(
            name=f"joint{index}",
            motor_id=index,
            position=DISCONNECTED_SENTINEL,
            velocity=0.0,
            torque=0.0,
            motor_time=0.0,
            mode=int(FrameMode.STOP),
            fault=0,
        )
        for index in range(1, EXPECTED_MOTOR_COUNT + 1)
    ]


def _raise_limit_violation(values: np.ndarray, lower: np.ndarray, upper: np.ndarray, label: str) -> None:
    below = values < lower
    above = values > upper
    if not np.any(below | above):
        return
    index = int(np.flatnonzero(below | above)[0])
    name = f"joint{index + 1}" if index < 6 else "gripper"
    direction = "下限" if below[index] else "上限"
    limit = lower[index] if below[index] else upper[index]
    raise LimitViolationError(f"{name} {label} {values[index]:.6g} 超过{direction} {limit:.6g}")


def _raise_magnitude_violation(values: Sequence[float], limits: Sequence[float], label: str) -> None:
    array = np.asarray(values, dtype=np.float64)
    limit_array = np.asarray(limits, dtype=np.float64)
    exceeded = np.abs(array) > limit_array
    if not np.any(exceeded):
        return
    index = int(np.flatnonzero(exceeded)[0])
    name = f"joint{index + 1}" if index < 6 else "gripper"
    raise LimitViolationError(f"{name} {label} {array[index]:.6g} 超过限值 ±{limit_array[index]:.6g}")
