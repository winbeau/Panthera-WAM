"""在隔离的 LeRobot 环境中把一条示教 JSONL 导出为 LeRobotDataset v3。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .teach import load_raw_frames

AXES = ["joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper"]


def feature_spec() -> dict[str, dict]:
    names = {"axes": AXES}
    return {
        "observation.state": {"dtype": "float32", "shape": (7,), "names": names},
        "observation.velocity": {"dtype": "float32", "shape": (7,), "names": names},
        "action": {"dtype": "float32", "shape": (7,), "names": names},
        "action.velocity": {"dtype": "float32", "shape": (7,), "names": names},
        "panthera.timestamp": {"dtype": "float32", "shape": (1,), "names": None},
    }


def estimate_fps(frames: list[dict]) -> int:
    timestamps = np.asarray([frame["t"] for frame in frames], dtype=np.float64)
    deltas = np.diff(timestamps)
    deltas = deltas[deltas > 1e-6]
    return max(1, round(1.0 / float(np.median(deltas)))) if len(deltas) else 1


def lerobot_frame(frame: dict, task: str) -> dict:
    state = np.asarray(
        [*frame["pos"], float(frame.get("gripper_pos", 0.0))],
        dtype=np.float32,
    )
    velocity = np.asarray(
        [*frame.get("vel", [0.0] * 6), float(frame.get("gripper_vel", 0.0))],
        dtype=np.float32,
    )
    return {
        "observation.state": state,
        "observation.velocity": velocity,
        # 示教记录没有另一条控制目标流；位置/速度示范本身就是监督 action。
        "action": state.copy(),
        "action.velocity": velocity.copy(),
        "panthera.timestamp": np.asarray([frame["t"]], dtype=np.float32),
        "task": task,
    }


def export_dataset(
    trajectory: Path,
    output: Path,
    *,
    repo_id: str,
    task: str,
) -> int:
    from lerobot.datasets import LeRobotDataset

    frames = load_raw_frames(trajectory)
    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        root=output,
        robot_type="panthera_ht_d405",
        fps=estimate_fps(frames),
        features=feature_spec(),
        use_videos=False,
    )
    try:
        total = len(frames)
        for index, frame in enumerate(frames, start=1):
            dataset.add_frame(lerobot_frame(frame, task))
            if index == total or index % max(1, total // 20) == 0:
                print(json.dumps({"progress": index / total, "frame_count": index}), flush=True)
        dataset.save_episode()
        dataset.finalize()
    except BaseException:
        # v3 writer 必须 finalize，成功路径与异常路径都尽量关闭 parquet writer。
        try:
            dataset.finalize()
        except Exception:
            pass
        raise

    (output / "panthera-source.json").write_text(
        json.dumps(
            {
                "format": "LeRobotDataset v3.0",
                "source_trajectory": str(trajectory),
                "task": task,
                "mapping": {
                    "pos + gripper_pos": "observation.state / action",
                    "vel + gripper_vel": "observation.velocity / action.velocity",
                    "t": "panthera.timestamp",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return len(frames)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    frame_count = export_dataset(
        args.trajectory,
        args.output,
        repo_id=args.repo_id,
        task=args.task,
    )
    print(
        json.dumps(
            {"progress": 1.0, "frame_count": frame_count, "output_dir": str(args.output)},
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
