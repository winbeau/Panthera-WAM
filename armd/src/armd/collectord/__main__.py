from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
from pathlib import Path

from .collector import CollectorAborted, CollectorConfig, collect_episode, load_json
from .schema import FPS


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
    parser.add_argument(
        "--duration-s",
        type=float,
        default=None,
        help="采集窗口时长（非定长模式；默认 5 s）",
    )
    parser.add_argument(
        "--fixed-duration-s",
        type=float,
        default=None,
        help="定长输出的观测时长；30 s 会产出 901 个 canonical tick、900 个训练 frame",
    )
    parser.add_argument(
        "--fixed-ticks",
        type=int,
        default=None,
        help="固定输出 canonical tick 数（低级接口；例如 901 对应 900 个训练 frame）",
    )
    parser.add_argument(
        "--fixed-margin-s",
        type=float,
        default=5.0,
        help="定长模式额外采集的对齐余量（秒，默认 5）",
    )
    parser.add_argument("--capture-depth", action="store_true")
    parser.add_argument("--expected-overhead-serial", default="")
    parser.add_argument("--expected-wrist-serial", default="260422273428")
    parser.add_argument(
        "--stage-workers",
        type=int,
        default=int(os.environ.get("PANTHERA_COLLECTORD_STAGE_WORKERS", "2")),
        help="收尾阶段（staging/校验）并行编码 worker 数，1..4，默认 2（2 核并行，预期 1.3-1.5×）",
    )
    parser.add_argument(
        "--nice",
        type=int,
        default=int(os.environ.get("PANTHERA_COLLECTORD_NICE", "10")),
        help="collectord 进程 nice 值（默认 10，降权；避免收尾并行抢 armd 200Hz 实时循环的 CPU）",
    )
    parser.add_argument(
        "--sim-allow-unapproved-root",
        action="store_true",
        help="tests/simulation only; production requires .panthera-usb3-ssd.json",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    if args.fixed_duration_s is not None and args.fixed_ticks is not None:
        raise SystemExit("--fixed-duration-s 与 --fixed-ticks 不能同时使用")
    if args.fixed_duration_s is not None and args.duration_s is not None:
        raise SystemExit("定长模式不能同时指定 --duration-s")
    if args.fixed_margin_s < 0:
        raise SystemExit("--fixed-margin-s 不能为负")

    if not 1 <= args.stage_workers <= 4:
        raise SystemExit("--stage-workers 必须在 1..4 之间")
    fixed_ticks = args.fixed_ticks
    if args.fixed_duration_s is not None:
        if args.fixed_duration_s <= 0:
            raise SystemExit("--fixed-duration-s 必须大于 0")
        frame_count = round(args.fixed_duration_s * FPS)
        if not math.isclose(args.fixed_duration_s * FPS, frame_count, abs_tol=1e-6):
            raise SystemExit(f"--fixed-duration-s 必须是 {1 / FPS:g} s 的整数倍")
        fixed_ticks = frame_count + 1
        duration_s = args.fixed_duration_s + args.fixed_margin_s
    elif fixed_ticks is not None:
        target_duration_s = (fixed_ticks - 1) / FPS
        duration_s = target_duration_s + args.fixed_margin_s if args.duration_s is None else args.duration_s
    else:
        duration_s = 5.0 if args.duration_s is None else args.duration_s

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
        duration_s=duration_s,
        fixed_ticks=fixed_ticks,
        capture_depth=args.capture_depth,
        allow_unapproved_root=args.sim_allow_unapproved_root,
        expected_overhead_serial=args.expected_overhead_serial,
        expected_wrist_serial=args.expected_wrist_serial,
        stage_workers=args.stage_workers,
    )
    # 降权（仅 Unix）：collectord 收尾阶段的 2 核并行不得饥饿 armd 的
    # 200Hz HardwareLoop（固件 150ms 看门狗）。nice>0 是降权。
    try:
        os.nice(args.nice)
    except (AttributeError, OSError, PermissionError):
        pass
    finish_event = asyncio.Event()
    abort_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signum, handler in (
        (signal.SIGUSR1, finish_event.set),
        (signal.SIGINT, abort_event.set),
        (signal.SIGTERM, abort_event.set),
    ):
        try:
            loop.add_signal_handler(signum, handler)
            installed_signals.append(signum)
        except (AttributeError, NotImplementedError, RuntimeError):
            pass
    try:
        path = await collect_episode(
            config,
            finish_event=finish_event,
            abort_event=abort_event,
        )
    except CollectorAborted as exc:
        print(json.dumps({"status": "aborted", "reason": str(exc)}))
        raise SystemExit(2) from exc
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
    print(json.dumps({"episode": str(path), "status": "complete"}))


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
