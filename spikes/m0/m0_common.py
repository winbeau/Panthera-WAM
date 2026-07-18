"""M0 spike 公共工具。

设计要点：
- **不实例化 `Panthera`**。`Panthera.__init__` 会调 `htr.Robot.__init__` 连电机，
  纯计算类 spike 不需要、也不应该碰硬件。这里改为：从配置直接构建 pinocchio
  模型，再把 `Panthera` 上**真实的**运动学方法借到一个无硬件的壳对象上，
  从而测的是 SDK 原版算法而不是复刻品。
- 硬件类 spike 统一走 `confirm_motion()` 安全闸（先打印将要执行的动作，再二次确认）。
"""
from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pinocchio as pin
import yaml

# Panthera_lib 是源码分发，官方约定需从 scripts 目录导入
DEFAULT_SDK_ROOT = Path(os.environ.get("PANTHERA_SDK_ROOT", Path.home() / "Panthera-HT_SDK"))


def sdk_paths(sdk_root: Path | None = None) -> tuple[Path, Path]:
    """返回 (scripts_dir, robot_param_dir)。"""
    root = Path(sdk_root or DEFAULT_SDK_ROOT)
    py = root / "panthera_python"
    if not py.is_dir():
        raise SystemExit(f"找不到 SDK: {py}\n请设置 PANTHERA_SDK_ROOT 指向 Panthera-HT_SDK 克隆目录")
    return py / "scripts", py / "robot_param"


def load_config(config_name: str = "Follower.yaml", sdk_root: Path | None = None) -> tuple[dict, Path]:
    """加载机械臂配置，返回 (config, config_dir)。"""
    _, param_dir = sdk_paths(sdk_root)
    config_path = param_dir / config_name
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f), config_path.parent


@dataclass
class RobotModel:
    """一份**独立的** pinocchio 模型实例（含其 data）。"""

    model: Any
    data: Any
    joint_names: list[str]
    joint_ids: list[int]
    end_effector_frame_id: int
    joint_limits: dict[str, np.ndarray]
    motor_count: int
    urdf_path: Path


def build_model(config_name: str = "Follower.yaml", sdk_root: Path | None = None) -> RobotModel:
    """复刻 `Panthera._load_urdf_model` 的构建过程，但完全不碰电机。

    这是 M0-3 的核心：每次调用都应产出一份彼此独立的模型。
    """
    config, config_dir = load_config(config_name, sdk_root)
    urdf_path = (config_dir / config["urdf"]["file_path"]).resolve()
    if not urdf_path.is_file():
        raise SystemExit(f"URDF 不存在: {urdf_path}")

    model = pin.buildModelFromUrdf(str(urdf_path))
    data = model.createData()

    joint_names = list(config["kinematics"]["joint_names"])
    joint_ids = [model.getJointId(n) for n in joint_names if model.existJointName(n)]

    eef_link = config["urdf"]["end_effector_link"]
    if model.existFrame(eef_link):
        eef_id = model.getFrameId(eef_link)
    else:
        eef_id = model.getFrameId(joint_names[-1])

    limits = {
        "lower": np.array(config["robot"]["joint_limits"]["lower"], dtype=float),
        "upper": np.array(config["robot"]["joint_limits"]["upper"], dtype=float),
    }

    return RobotModel(
        model=model,
        data=data,
        joint_names=joint_names,
        joint_ids=joint_ids,
        end_effector_frame_id=eef_id,
        joint_limits=limits,
        motor_count=len(joint_names),
        urdf_path=urdf_path,
    )


# 需要从 Panthera 借用的纯计算方法（均只依赖 self.model/self.data，不碰电机）
_BORROWED = (
    "forward_kinematics",
    "get_jacobian",
    "get_manipulability",
    "inverse_kinematics",
    "_inverse_kinematics_dls_single_impl",
    "_inverse_kinematics_dls_multi_init_impl",
    "get_Gravity",
    "get_Coriolis",
    "get_Mass_Matrix",
    "get_Dynamics",
)


def make_kin_shim(rm: RobotModel, current_q: np.ndarray | None = None):
    """把 `Panthera` 上真实的运动学/动力学方法借到一个无硬件壳对象上。

    为什么这么做而不是复刻算法：M0-3 要表征的是 **SDK 实际会跑的那段代码**
    的耗时与并发行为，复刻品的性能特征没有参考价值。

    `get_current_pos` 被替换成固定值——SDK 的 multi_init 分支
    (`Panthera.py:892`) 会调用它，而真实实现要读电机。
    """
    scripts_dir, _ = sdk_paths()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from Panthera_lib import Panthera  # noqa: E402  仅导入类，不实例化

    shim = types.SimpleNamespace()
    shim.model = rm.model
    shim.data = rm.data
    shim.joint_names = rm.joint_names
    shim.joint_ids = rm.joint_ids
    shim.end_effector_frame_id = rm.end_effector_frame_id
    shim.joint_limits = rm.joint_limits
    shim.motor_count = rm.motor_count

    q0 = np.zeros(rm.motor_count) if current_q is None else np.asarray(current_q, dtype=float)
    shim.get_current_pos = lambda: q0.copy()
    shim.get_current_vel = lambda: np.zeros(rm.motor_count)

    for name in _BORROWED:
        fn = getattr(Panthera, name, None)
        if fn is None:
            raise SystemExit(f"SDK 中找不到方法 {name}，Panthera.py 可能已变更")
        setattr(shim, name, types.MethodType(fn, shim))

    return shim


def confirm_motion(title: str, actions: list[str], *, joint_limits: dict | None = None) -> None:
    """真机运动安全闸：先完整打印将要执行的动作，再要求显式输入确认。

    对应 CLAUDE.md 安全红线第 4 条。任何会让机械臂动起来的 spike 都必须先过这一关。
    """
    bar = "=" * 68
    print(f"\n{bar}\n⚠  即将对**真实机械臂**执行以下动作：{title}\n{bar}")
    for i, a in enumerate(actions, 1):
        print(f"  {i}. {a}")
    if joint_limits is not None:
        print(f"\n  关节限位 lower: {joint_limits['lower']}")
        print(f"  关节限位 upper: {joint_limits['upper']}")
    print(f"""
  请确认：
    - 操作者在场，手可及急停/电源
    - 机械臂周围无人无障碍物
    - 已扶稳机械臂（掉电会跌落）
{bar}""")
    if os.environ.get("PANTHERA_SPIKE_ASSUME_YES") == "1":
        print("PANTHERA_SPIKE_ASSUME_YES=1，跳过交互确认")
        return
    reply = input("输入大写 YES 继续，其它任意键中止： ").strip()
    if reply != "YES":
        raise SystemExit("已中止，未向机械臂发送任何指令。")


def percentiles(xs: list[float]) -> dict[str, float]:
    a = np.asarray(xs, dtype=float)
    return {
        "p50": float(np.percentile(a, 50)),
        "p95": float(np.percentile(a, 95)),
        "p99": float(np.percentile(a, 99)),
        "max": float(a.max()),
        "mean": float(a.mean()),
    }
