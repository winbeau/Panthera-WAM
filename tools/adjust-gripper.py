#!/usr/bin/env python3
"""把录制动作中的夹爪轴按全量程百分比「调紧」。

轴说明（panthera-fastwam-v1 契约，7 轴顺序固定）：
  joint_1 joint_2 joint_3 joint_4 joint_5 joint_6 gripper
夹爪是第 7 轴（电机 J7，数值 0.0=全闭 / 2.0=全开）；J4 是腕俯仰关节。
默认只改 gripper 轴；--axis 可按名字（joint_1..joint_6 / gripper）或
0 基下标选择其它轴（仅用于诊断）。

语义：--pct 5 表示向「闭」方向收紧全量程的 5%：
  new = max(0.0, old - 0.05 * 2.0) = old - 0.1
开爪 1.8 → 1.7；闭爪 0.2 → 0.1。绝不覆盖原始文件，输出写新文件。

支持两种输入：
  - preview 轨迹 jsonl（trajectory_*.jsonl / replay_trajectory_*.jsonl）：
    gripper 轴字段为 gripper_pos；关节轴字段为 pos[i]。
  - 旧式 LeRobot 导出帧 jsonl：gripper 轴字段 gripper_pos，同上。
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

AXES = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")
GRIPPER_LOWER = 0.0
GRIPPER_UPPER = 2.0


def parse_axis(value: str) -> int:
    if value.isdigit():
        index = int(value)
        if not 0 <= index < len(AXES):
            raise SystemExit(f"axis 下标必须在 0..{len(AXES) - 1}")
        return index
    if value not in AXES:
        raise SystemExit(f"未知轴名 {value!r}；可选: {', '.join(AXES)}")
    return AXES.index(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把录制动作中的指定轴按全量程百分比收紧（默认夹爪 gripper）"
    )
    parser.add_argument("input", type=Path, help="轨迹 jsonl（preview trajectory / replay_trajectory）")
    parser.add_argument("--pct", type=float, required=True, help="收紧百分比（全量程的百分比，例如 5）")
    parser.add_argument("--axis", default="gripper", help="轴名（joint_1..joint_6、gripper）或 0 基下标，默认 gripper")
    parser.add_argument("--out", type=Path, help="输出路径；默认 <stem>.tighten<pct>pct.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    return parser.parse_args()


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number} 不是合法 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise SystemExit(f"{path}:{line_number} 不是 JSON 对象")
        rows.append(value)
    if not rows:
        raise SystemExit(f"{path} 没有有效帧")
    return rows


def axis_value(row: dict, index: int) -> float:
    if index == 6:
        raw = row.get("gripper_pos")
        if raw is None:
            raise SystemExit(f"帧缺少 gripper_pos 字段: {row}")
        return float(raw)
    raw = row.get("pos")
    if not isinstance(raw, (list, tuple)) or len(raw) != 6:
        raise SystemExit(f"帧缺少 6 关节 pos 字段: {row}")
    return float(raw[index])


def set_axis_value(row: dict, index: int, value: float) -> None:
    if index == 6:
        row["gripper_pos"] = value
    else:
        row["pos"][index] = value


def main() -> int:
    args = parse_args()
    axis_index = parse_axis(args.axis)
    if not math.isfinite(args.pct) or args.pct < 0:
        raise SystemExit("--pct 必须是非负有限数值")
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"输入不存在: {source}")
    rows = load_rows(source)

    shift = args.pct / 100.0 * (GRIPPER_UPPER - GRIPPER_LOWER)
    before = [axis_value(row, axis_index) for row in rows]
    after: list[float] = []
    for row, value in zip(rows, before, strict=True):
        new_value = value - shift
        if axis_index == 6:
            new_value = max(GRIPPER_LOWER, min(GRIPPER_UPPER, new_value))
        after.append(new_value)
        set_axis_value(row, axis_index, new_value)

    label = AXES[axis_index]
    print(f"轴: {label}（{axis_index}）  收紧 {args.pct:g}% 全量程 = 每帧 -{shift:.4g}")
    print(f"帧数: {len(rows)}  修改前 min/max/mean = "
          f"{min(before):.4f}/{max(before):.4f}/{sum(before) / len(before):.4f}")
    print(f"                 修改后 min/max/mean = "
          f"{min(after):.4f}/{max(after):.4f}/{sum(after) / len(after):.4f}")
    if args.dry_run:
        print("--dry-run：不写文件")
        return 0

    output = args.out or source.with_name(f"{source.stem}.tighten{args.pct:g}pct{source.suffix}")
    output = output.expanduser().resolve()
    if output == source:
        raise SystemExit("输出路径不能与输入相同（拒绝覆盖原始数据）")
    backup = source.with_suffix(source.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(source, backup)
        print(f"原始数据已备份: {backup}")
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"已写入: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
