"""应用层工作零位持久化（work-zero 方案 WZ-1，docs/FINAL_PLAN.md）。

工作零位是**应用层工作姿态**，不是硬件 encoder zero：

- 本模块不调用 SDK `set_reset_zero()` / `backend.set_zero()` / 既有 `SetZero` RPC；
- 文件由 armd 所在主机持有，客户端不直接读写 Pi 上的工作零位文件；
- 原子写（同目录临时文件 + fsync + os.replace + 目录 fsync + chmod 0600）；
- 加载时拒绝：损坏 JSON、未知 schema、缺字段、NaN/Infinity、维度错误、
  权限不可读、非普通文件；软限位校验由调用方提供 limits 后执行；
- `stream_instance_id` 变化不使工作零位失效：它只是来源元数据。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

WORK_ZERO_SCHEMA_VERSION = 1
DEFAULT_WORK_ZERO_PATH = "~/.config/panthera-wam/work-zero.json"
WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK = "teach-clutch-lock"


class WorkZeroValidationError(ValueError):
    """结构化拒绝原因：JSON 损坏、schema、维度、有限性、软限位等。"""


def _reject_constant(value: str) -> None:
    # Python json 默认接受 NaN/Infinity；工作零位文件必须显式拒绝。
    raise ValueError(f"不允许的非有限数值常量: {value}")


def _as_int(value: object, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WorkZeroValidationError(f"{name} 必须是整数") from exc


@dataclass(frozen=True, slots=True)
class WorkZeroPose:
    """7 轴工作姿态：6 个关节 + 夹爪，全部来自同一 lock 状态样本。"""

    schema_version: int
    joints: tuple[float, ...]  # exactly 6
    gripper: float
    captured_at_ms: int
    sampled_monotonic_ns: int | None
    state_sequence: int | None
    stream_instance_id: str
    source: str  # 第一版固定 teach-clutch-lock

    def __post_init__(self) -> None:
        if self.schema_version != WORK_ZERO_SCHEMA_VERSION:
            raise WorkZeroValidationError(
                f"未知 schema_version: {self.schema_version}（当前支持 {WORK_ZERO_SCHEMA_VERSION}）"
            )
        if len(self.joints) != 6:
            raise WorkZeroValidationError(f"joints 必须恰好 6 个，实际 {len(self.joints)}")
        values = (*self.joints, self.gripper)
        if not all(np.isfinite(value) for value in values):
            raise WorkZeroValidationError("joints/gripper 必须全部为有限数值")
        if self.source != WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK:
            raise WorkZeroValidationError(f"未知 source: {self.source!r}")
        if not isinstance(self.stream_instance_id, str):
            raise WorkZeroValidationError("stream_instance_id 必须为字符串")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "joints": list(self.joints),
            "gripper": self.gripper,
            "captured_at_ms": self.captured_at_ms,
            "sampled_monotonic_ns": self.sampled_monotonic_ns,
            "state_sequence": self.state_sequence,
            "stream_instance_id": self.stream_instance_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: object) -> WorkZeroPose:
        if not isinstance(data, dict):
            raise WorkZeroValidationError("工作零位文件必须是 JSON 对象")
        required = {
            "schema_version",
            "joints",
            "gripper",
            "captured_at_ms",
            "stream_instance_id",
            "source",
        }
        missing = required - data.keys()
        if missing:
            raise WorkZeroValidationError(f"缺少字段: {sorted(missing)}")
        try:
            joints = tuple(float(value) for value in data["joints"])
        except (TypeError, ValueError) as exc:
            raise WorkZeroValidationError("joints 必须是数值数组") from exc
        try:
            gripper = float(data["gripper"])
        except (TypeError, ValueError) as exc:
            raise WorkZeroValidationError("gripper 必须是数值") from exc
        sampled = data.get("sampled_monotonic_ns")
        state_sequence = data.get("state_sequence")
        return cls(
            schema_version=_as_int(data["schema_version"], "schema_version"),
            joints=joints,
            gripper=gripper,
            captured_at_ms=_as_int(data["captured_at_ms"], "captured_at_ms"),
            sampled_monotonic_ns=_as_int(sampled, "sampled_monotonic_ns") if sampled is not None else None,
            state_sequence=_as_int(state_sequence, "state_sequence") if state_sequence is not None else None,
            stream_instance_id=str(data["stream_instance_id"]),
            source=str(data["source"]),
        )


class WorkZeroStore:
    """路径解析、加载、校验、原子保存；不读 Backend，不触碰 SDK。"""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = os.environ.get("PANTHERA_WORK_ZERO_PATH") or DEFAULT_WORK_ZERO_PATH
        self._path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> WorkZeroPose | None:
        """只读加载；文件不存在返回 None（exists=false 是正常结果）。"""
        path = self._path
        if not path.exists():
            return None
        if not path.is_file():
            raise WorkZeroValidationError("工作零位路径不是普通文件")
        if not os.access(path, os.R_OK):
            raise WorkZeroValidationError("工作零位文件不可读")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkZeroValidationError(f"工作零位文件读取失败: {exc}") from exc
        try:
            data = json.loads(raw, parse_constant=_reject_constant)
        except ValueError as exc:
            raise WorkZeroValidationError(f"工作零位 JSON 损坏: {exc}") from exc
        return WorkZeroPose.from_dict(data)

    def validate_with_limits(self, pose: WorkZeroPose, limits) -> None:
        """软限位校验；limits 由调用方从 Backend 读取（本模块不碰 SDK）。"""
        joints = np.asarray(pose.joints, dtype=np.float64)
        if np.any(joints < limits.joint_lower) or np.any(joints > limits.joint_upper):
            index = int(
                np.flatnonzero(
                    (joints < limits.joint_lower) | (joints > limits.joint_upper)
                )[0]
            )
            direction = "下限" if joints[index] < limits.joint_lower[index] else "上限"
            bound = (
                limits.joint_lower[index] if joints[index] < limits.joint_lower[index] else limits.joint_upper[index]
            )
            raise WorkZeroValidationError(
                f"joint{index + 1} 工作零位 {joints[index]:.6g} 超过{direction} {bound:.6g}"
            )
        if not (limits.gripper_lower <= pose.gripper <= limits.gripper_upper):
            raise WorkZeroValidationError(
                f"gripper 工作零位 {pose.gripper:.6g} 超出软限位 "
                f"[{limits.gripper_lower:.6g}, {limits.gripper_upper:.6g}]"
            )

    def save(self, pose: WorkZeroPose, limits) -> None:
        """原子保存；写入前先做软限位校验，绝不允许越限姿态进入文件。"""
        self.validate_with_limits(pose, limits)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(pose.to_dict(), ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
            # 目录 fsync（平台允许时），保证 rename 持久化。
            try:
                dir_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
