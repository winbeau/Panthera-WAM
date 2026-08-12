from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .collector import CollectorConfig, collect_episode, load_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect a loss-aware Panthera training episode")
    parser.add_argument("--arm-endpoint", default="127.0.0.1:50051")
    parser.add_argument("--wrist-endpoint", default="127.0.0.1:50052")
    parser.add_argument("--overhead-endpoint", default="127.0.0.1:50053")
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--panthera-commit", required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--duration-s", type=float, default=5.0)
    parser.add_argument("--capture-depth", action="store_true")
    parser.add_argument("--expected-overhead-serial", default="")
    parser.add_argument("--expected-wrist-serial", default="260422273428")
    parser.add_argument(
        "--sim-allow-unapproved-root",
        action="store_true",
        help="tests/simulation only; production requires .panthera-usb3-ssd.json",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    identity = load_json(args.identity)
    required_identity = {
        "dataset_id",
        "task_id",
        "calibration_version",
        "camera_mount_version",
        "roi_version",
        "action_units_version",
    }
    missing = sorted(required_identity - identity.keys())
    if missing:
        raise SystemExit(f"identity JSON is missing: {missing}")
    config = CollectorConfig(
        arm_endpoint=args.arm_endpoint,
        overhead_endpoint=args.overhead_endpoint,
        wrist_endpoint=args.wrist_endpoint,
        collection_root=args.collection_root,
        episode_id=args.episode_id,
        canonical_task=args.task,
        operator=args.operator,
        panthera_wam_commit=args.panthera_commit,
        calibration=load_json(args.calibration),
        identity={key: str(value) for key, value in identity.items()},
        duration_s=args.duration_s,
        capture_depth=args.capture_depth,
        allow_unapproved_root=args.sim_allow_unapproved_root,
        expected_overhead_serial=args.expected_overhead_serial,
        expected_wrist_serial=args.expected_wrist_serial,
    )
    path = asyncio.run(collect_episode(config))
    print(json.dumps({"episode": str(path), "status": "complete"}))


if __name__ == "__main__":
    main()
