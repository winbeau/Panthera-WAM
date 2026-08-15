#!/usr/bin/env python3
"""Record a preview as two continuous MP4s plus a replayable joint trajectory.

Run on Pi 5:
    ./deploy/preview-record.sh color-block 001

Outputs:
    ~/panthera-data/preview/color-block_001/
      color-block_wrist_001.mp4
      color-block_overhead_001.mp4
      trajectory_001.jsonl
      preview.json

The same trajectory is copied into the armd TeachStore under:
    ~/.local/share/panthera/teach/preview/color-block_001/trajectory_001.jsonl

This process only reads camera/state streams. It does not acquire the arm
lease and does not send movement commands; manual movement remains controlled
by the running teach session.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import signal
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import grpc
import numpy as np
from PIL import Image
from panthera_arm import arm_pb2, arm_pb2_grpc, camera_pb2, camera_pb2_grpc

DEFAULT_ROOT = Path.home() / "panthera-data" / "preview"
DEFAULT_TEACH_ROOT = Path.home() / ".local" / "share" / "panthera" / "teach"
DEFAULT_RATE_HZ = 15.0
DEFAULT_DURATION_S = 30.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="连续录制 preview：双路 MP4 + 关节轨迹")
    parser.add_argument("task", help="任务名，例如 color-block")
    parser.add_argument("number", help="三位编号，例如 001")
    parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--rate-hz", type=float, default=DEFAULT_RATE_HZ)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--teach-root", type=Path, default=DEFAULT_TEACH_ROOT)
    parser.add_argument("--arm-endpoint", default="127.0.0.1:50051")
    parser.add_argument("--wrist-endpoint", default="127.0.0.1:50052")
    parser.add_argument("--overhead-endpoint", default="127.0.0.1:50053")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.task):
        raise SystemExit("task 只能包含字母、数字、点、下划线和连字符")
    if not re.fullmatch(r"\d{3}", args.number):
        raise SystemExit("number 必须是三位数字，例如 001")
    if args.duration_s <= 0:
        raise SystemExit("duration-s 必须大于 0")
    if not 1.0 <= args.rate_hz <= 30.0:
        raise SystemExit("rate-hz 必须在 1–30 之间")


def frame_to_video_frame(frame: Any) -> av.VideoFrame | None:
    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_JPEG:
        with Image.open(io.BytesIO(frame.data)) as image:
            return av.VideoFrame.from_image(image.convert("RGB"))

    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_RGB8:
        stride = frame.stride or frame.width * 3
        raw = np.frombuffer(frame.data, dtype=np.uint8)
        needed = frame.height * stride
        if raw.size < needed:
            return None
        rgb = raw[:needed].reshape(frame.height, stride)[:, : frame.width * 3]
        rgb = rgb.reshape(frame.height, frame.width, 3)
        return av.VideoFrame.from_ndarray(rgb, format="rgb24")

    return None


def encode_camera(
    name: str,
    endpoint: str,
    output: Path,
    rate_hz: float,
    start_event: threading.Event,
    stop_event: threading.Event,
    errors: list[str],
    counts: dict[str, int],
) -> None:
    channel = grpc.insecure_channel(
        endpoint,
        options=[
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ],
    )
    container: av.container.OutputContainer | None = None
    stream: Any = None
    call = None
    count = 0
    try:
        stub = camera_pb2_grpc.CameraServiceStub(channel)
        status = stub.GetStatus(camera_pb2.CameraStatusRequest(), timeout=5.0)
        if not status.available or not status.streaming:
            raise RuntimeError(f"{name} camera unavailable: {status.error or 'not streaming'}")
        start_event.wait()
        call = stub.StreamFrames(
            camera_pb2.StreamFramesRequest(
                stream=camera_pb2.CAMERA_STREAM_TYPE_COLOR,
                max_rate_hz=rate_hz,
                max_frames=0,
            )
        )
        fps = Fraction(str(rate_hz)).limit_denominator(1000)
        for frame in call:
            if stop_event.is_set():
                break
            video_frame = frame_to_video_frame(frame)
            if video_frame is None:
                continue
            if container is None:
                output.parent.mkdir(parents=True, exist_ok=True)
                container = av.open(str(output), mode="w")
                stream = container.add_stream("h264", rate=fps)
                stream.width = video_frame.width
                stream.height = video_frame.height
                stream.pix_fmt = "yuv420p"
                stream.options = {"preset": "veryfast", "crf": "23"}
            video_frame.pts = count
            for packet in stream.encode(video_frame):
                container.mux(packet)
            count += 1
    except grpc.RpcError as exc:
        if not stop_event.is_set():
            errors.append(f"{name}: gRPC {exc.code().name}: {exc.details()}")
            stop_event.set()
    except Exception as exc:  # noqa: BLE001 - report worker failure to main thread
        errors.append(f"{name}: {exc}")
        stop_event.set()
    finally:
        if call is not None:
            call.cancel()
        if stream is not None and container is not None:
            for packet in stream.encode():
                container.mux(packet)
        if container is not None:
            container.close()
        channel.close()
        counts[name] = count


def record_state(
    endpoint: str,
    output: Path,
    start_event: threading.Event,
    stop_event: threading.Event,
    errors: list[str],
    counts: dict[str, int],
) -> None:
    channel = grpc.insecure_channel(
        endpoint,
        options=[
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ],
    )
    call = None
    handle = None
    count = 0
    first_timestamp: int | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = output.open("w", encoding="utf-8")
        start_event.wait()
        stub = arm_pb2_grpc.ArmServiceStub(channel)
        call = stub.StreamMeasuredState(
            arm_pb2.StreamMeasuredStateRequest(start_at_latest=True)
        )
        for message in call:
            if stop_event.is_set():
                break
            robot = message.state
            motors = [*robot.joint.joints, robot.gripper.state]
            if len(motors) != 7 or not all(motor.valid for motor in motors):
                continue
            timestamp = int(robot.sampled_monotonic_ns)
            if first_timestamp is None:
                first_timestamp = timestamp
            item = {
                "t": max(0.0, (timestamp - first_timestamp) / 1_000_000_000.0),
                "pos": [float(motor.position) for motor in motors[:6]],
                "vel": [float(motor.velocity) for motor in motors[:6]],
                # SDK 开位偶尔会返回略小于 0 的量化值；TeachPlay 的
                # 契约是 gripper position ∈ [0, 2]、velocity ∈ [-1, 1]。
                "gripper_pos": float(np.clip(motors[6].position, 0.0, 2.0)),
                "gripper_vel": float(np.clip(motors[6].velocity, -1.0, 1.0)),
            }
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
            if count % 50 == 0:
                handle.flush()
    except grpc.RpcError as exc:
        if not stop_event.is_set():
            errors.append(f"state: gRPC {exc.code().name}: {exc.details()}")
            stop_event.set()
    except Exception as exc:  # noqa: BLE001 - report worker failure to main thread
        errors.append(f"state: {exc}")
        stop_event.set()
    finally:
        if call is not None:
            call.cancel()
        if handle is not None:
            handle.flush()
            handle.close()
        channel.close()
        counts["state"] = count


def main() -> int:
    args = parse_args()
    validate_args(args)
    session = f"{args.task}_{args.number}"
    output_dir = args.root.expanduser().resolve() / session
    wrist_output = output_dir / f"{args.task}_wrist_{args.number}.mp4"
    overhead_output = output_dir / f"{args.task}_overhead_{args.number}.mp4"
    trajectory_output = output_dir / f"trajectory_{args.number}.jsonl"
    metadata_output = output_dir / "preview.json"
    teach_relative = Path("preview") / session / trajectory_output.name
    teach_output = args.teach_root.expanduser().resolve() / teach_relative

    if output_dir.exists() and not args.overwrite:
        raise SystemExit(f"输出目录已存在：{output_dir}；需要覆盖时加 --overwrite")
    if args.overwrite and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    start_event = threading.Event()
    stop_event = threading.Event()
    errors: list[str] = []
    counts: dict[str, int] = {}
    threads = [
        threading.Thread(
            target=record_state,
            args=(args.arm_endpoint, trajectory_output, start_event, stop_event, errors, counts),
            daemon=True,
        ),
        threading.Thread(
            target=encode_camera,
            args=("wrist", args.wrist_endpoint, wrist_output, args.rate_hz, start_event, stop_event, errors, counts),
            daemon=True,
        ),
        threading.Thread(
            target=encode_camera,
            args=("overhead", args.overhead_endpoint, overhead_output, args.rate_hz, start_event, stop_event, errors, counts),
            daemon=True,
        ),
    ]

    def request_stop(signum, _frame) -> None:
        print(f"收到信号 {signum}，正在正常结束 preview...", flush=True)
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)

    print(f"输出目录：{output_dir}", flush=True)
    print(f"wrist：{wrist_output.name}", flush=True)
    print(f"overhead：{overhead_output.name}", flush=True)
    print(f"trajectory：{trajectory_output.name}（200 Hz measured state）", flush=True)
    print(f"目标：{args.duration_s:.1f}s @ {args.rate_hz:.1f} FPS；Ctrl-C 可正常收尾", flush=True)
    for thread in threads:
        thread.start()
    start_event.set()

    deadline = time.monotonic() + args.duration_s
    while time.monotonic() < deadline and not stop_event.is_set():
        time.sleep(0.1)
    stop_event.set()
    for thread in threads:
        thread.join(timeout=10.0)

    try:
        teach_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(trajectory_output, teach_output)
    except OSError as exc:
        errors.append(f"trajectory copy to TeachStore failed: {exc}")

    metadata = {
        "task": args.task,
        "number": args.number,
        "duration_s": args.duration_s,
        "camera_rate_hz": args.rate_hz,
        "frames": counts,
        "trajectory": str(trajectory_output),
        "teach_trajectory": str(teach_relative),
        "wrist_endpoint": args.wrist_endpoint,
        "overhead_endpoint": args.overhead_endpoint,
        "arm_endpoint": args.arm_endpoint,
        "success": not errors and all(counts.get(name, 0) > 0 for name in ("state", "wrist", "overhead")),
    }
    metadata_output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
