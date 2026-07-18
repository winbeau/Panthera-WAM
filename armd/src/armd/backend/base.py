"""armd 硬件后端抽象。

**接口设计的核心是把 N7 约束变成类型层面的强制**（FINAL_PLAN §V6-N7）：

    同一 CAN 端口上的 7 个电机共用一帧 TX，帧头只有一个模式；切模式会把其它
    电机的槽位抹成 0x8000（无指令）。因此「先写关节、再写夹爪」这种写法会
    静默清空关节指令。

所以本接口**不提供逐电机写入**，只提供 `write_frame()` ——一次调用必须给出
全部 7 个电机在**同一模式**下的指令。想漏写某个电机在类型上就做不到，
从而在编译/调用期就消灭 N7 类 bug，而不是靠开发者记得。

后端有两个实现：
    - `SimBackend` ：状态/运动语义 + 一阶电机模型，无需硬件，全部 pytest 走它
    - `RealBackend`：封装官方 SDK `Panthera`，零修改，仅在真机集成时使用

纯计算运动学按 FINAL_PLAN §1.4 放在独立进程 worker，不与硬件后端对象混用。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

# SDK 用 999.0 作为「电机未连接/无数据」哨兵（核实结论 §V4）
DISCONNECTED_SENTINEL = 999.0
# SDK 自身以 0.1s 判定状态是否新鲜（robot.cpp:208）
STALE_AFTER_S = 0.1


class BackendError(RuntimeError):
    """后端操作失败。"""


class BackendClosedError(BackendError):
    """后端已关闭。"""


class LimitViolationError(BackendError):
    """目标或连续运动将越过软限位。"""


class FrameMode(enum.IntEnum):
    """整帧控制模式，取值与固件 `MODE_*` 常量一致（serial_struct.hpp）。

    一帧只能有一个模式——这正是 N7 的根源。
    """

    STOP = 0x03
    BRAKE = 0x04
    VELOCITY = 0x81
    POS_VEL_TQE = 0x90  # pos_vel_MAXtqe：位置+速度+最大力矩
    POS_VEL_TQE_KP_KD = 0xB0  # MIT 五参数
    MOTOR_STATE2 = 0x0A  # 纯状态查询


def _vector(value: np.ndarray, *, name: str, length: int) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (length,):
        raise ValueError(f"{name} 长度必须为 {length}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} 必须全部为有限数值")
    array = array.copy()
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class BackendLimits:
    """armd 软限位基线；真机当前没有硬件限位保护（FINAL_PLAN §V6-N3）。"""

    joint_lower: np.ndarray
    joint_upper: np.ndarray
    joint_velocity: np.ndarray
    joint_torque: np.ndarray
    gripper_lower: float = 0.0
    gripper_upper: float = 2.0
    gripper_velocity: float = 1.0
    gripper_torque: float = 0.5

    def __post_init__(self) -> None:
        n_joints = len(np.asarray(self.joint_lower))
        lower = _vector(self.joint_lower, name="joint_lower", length=n_joints)
        upper = _vector(self.joint_upper, name="joint_upper", length=n_joints)
        velocity = _vector(self.joint_velocity, name="joint_velocity", length=n_joints)
        torque = _vector(self.joint_torque, name="joint_torque", length=n_joints)
        if np.any(lower >= upper):
            raise ValueError("joint_lower 必须逐项小于 joint_upper")
        if np.any(velocity <= 0) or np.any(torque <= 0):
            raise ValueError("关节速度/力矩限位必须为正数")
        if not self.gripper_lower < self.gripper_upper:
            raise ValueError("gripper_lower 必须小于 gripper_upper")
        if self.gripper_velocity <= 0 or self.gripper_torque <= 0:
            raise ValueError("夹爪速度/力矩限位必须为正数")
        object.__setattr__(self, "joint_lower", lower)
        object.__setattr__(self, "joint_upper", upper)
        object.__setattr__(self, "joint_velocity", velocity)
        object.__setattr__(self, "joint_torque", torque)


DEFAULT_LIMITS = BackendLimits(
    joint_lower=np.array([-2.4, -0.1, -0.1, -1.6, -1.7, -2.5]),
    joint_upper=np.array([2.4, 3.2, 4.0, 1.6, 1.7, 2.5]),
    joint_velocity=np.ones(6),
    joint_torque=np.array([21.0, 36.0, 36.0, 21.0, 10.0, 10.0]),
)


@dataclass(frozen=True, slots=True)
class MotorSnapshot:
    """与 C++ `motor_back_t` 逐字段对齐（核实结论 §V4）。

    注意没有 `online` 字段——SDK 里不存在，在线与否由 `valid` + `age_s` 判定。
    """

    name: str
    motor_id: int
    position: float
    velocity: float
    torque: float
    motor_time: float
    mode: int
    fault: int
    pos_limit_flag: int = 0
    tor_limit_flag: int = 0

    @property
    def valid(self) -> bool:
        """position == 999.0 是未连接哨兵，绝不能当真实位置外传。"""
        return abs(self.position - DISCONNECTED_SENTINEL) > 1e-6


@dataclass(frozen=True, slots=True)
class JointFrame:
    """一个控制周期要下发的**完整**一帧：7 个电机 + 单一模式。

    `arm_*` 长度必须为 6，`gripper_*` 为标量。夹爪指令必须表达成与关节
    相同的模式——两种表达（pos-vel / MIT）在 SDK 里都存在，故总是可达。
    """

    mode: FrameMode
    arm_position: np.ndarray
    arm_velocity: np.ndarray
    gripper_position: float
    gripper_velocity: float
    # POS_VEL_TQE 用
    arm_max_torque: np.ndarray | None = None
    gripper_max_torque: float = 0.5
    # POS_VEL_TQE_KP_KD (MIT) 用
    arm_torque: np.ndarray | None = None
    arm_kp: np.ndarray | None = None
    arm_kd: np.ndarray | None = None
    gripper_torque: float = 0.0
    gripper_kp: float = 5.0
    gripper_kd: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm_position", _vector(self.arm_position, name="arm_position", length=6))
        object.__setattr__(self, "arm_velocity", _vector(self.arm_velocity, name="arm_velocity", length=6))
        for field in ("arm_max_torque", "arm_torque", "arm_kp", "arm_kd"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _vector(value, name=field, length=6))

    def validate(self, n_joints: int = 6) -> None:
        if n_joints != 6:
            raise ValueError("当前 Panthera-HT 契约固定为 6 个关节")
        scalar_fields = (
            "gripper_position",
            "gripper_velocity",
            "gripper_max_torque",
            "gripper_torque",
            "gripper_kp",
            "gripper_kd",
        )
        if not all(np.isfinite(getattr(self, field)) for field in scalar_fields):
            raise ValueError("夹爪指令必须全部为有限数值")
        if self.mode is FrameMode.POS_VEL_TQE:
            if self.arm_max_torque is None:
                raise ValueError("POS_VEL_TQE 模式必须提供 arm_max_torque")
            if np.any(self.arm_velocity < 0) or self.gripper_velocity < 0:
                raise ValueError("POS_VEL_TQE 模式的速度参数必须为非负数")
            if np.any(self.arm_max_torque <= 0) or self.gripper_max_torque <= 0:
                raise ValueError("POS_VEL_TQE 模式的最大力矩必须为正数")
        if self.mode is FrameMode.POS_VEL_TQE_KP_KD:
            for field in ("arm_torque", "arm_kp", "arm_kd"):
                arr = getattr(self, field)
                if arr is None:
                    raise ValueError(f"MIT 模式下必须提供 {field}")
            if np.any(self.arm_kp < 0) or np.any(self.arm_kd < 0):
                raise ValueError("MIT 模式的 kp/kd 不得为负数")
            if self.gripper_kp < 0 or self.gripper_kd < 0:
                raise ValueError("MIT 模式的夹爪 kp/kd 不得为负数")
        if self.mode not in {
            FrameMode.STOP,
            FrameMode.BRAKE,
            FrameMode.VELOCITY,
            FrameMode.POS_VEL_TQE,
            FrameMode.POS_VEL_TQE_KP_KD,
        }:
            raise ValueError(f"{self.mode.name} 不是可下发的控制帧模式")


@runtime_checkable
class Backend(Protocol):
    """硬件后端。**所有方法只允许 HardwareLoop 线程调用**（共享 TX 帧无锁）。"""

    n_joints: int
    is_sim: bool
    limits: BackendLimits

    def refresh_state(self) -> None:
        """刷新电机状态缓存（真机上会发查询帧并 flush）。"""

    def read_all(self) -> list[MotorSnapshot]:
        """返回 7 个电机快照，索引 0..5 为关节，6 为夹爪。"""

    def write_frame(self, frame: JointFrame) -> None:
        """写入完整一帧并下发（内部只调一次 flush）。见本模块顶部 N7 说明。"""

    def stop(self) -> None:
        """急停。真机对应 `set_stop()`，其自带 flush（核实结论 §V3）。"""

    def set_zero(self, motor_ids: list[int] | None = None) -> tuple[bool, bool, str]:
        """重定义零点。**不产生运动**（§V2）。返回 (accepted, persisted, reason)。"""

    def close(self) -> None: ...
