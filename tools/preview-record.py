#!/usr/bin/env python3
"""Record a continuous wrist + overhead preview pair as MP4 files.

Run on Pi 5:
    ./deploy/preview-record.sh color-block 001

Outputs:
    <root>/color-block_001/color-block_wrist_001.mp4
    <root>/color-block_001/color-block_overhead_001.mp4

This is camera-only: it does not acquire the arm lease and does not send
movement commands.
"""
from __future__ import annotations

import argparse
import io
import json
import re
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
from panthera_arm import camera_pb2, camera_pb2_grpc

DEFAULT_ROOT = Path.home() / "panthera-data" / "preview"
DEFAULT_RATE_HZ = 15.0
DEFAULT_DURATION_S = 30.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="连续录制 wrist + overhead preview MP4")
    parser.add_argument("task", help="任务名，例如 color-block")
    parser.add_argument("number", help="三位编号，例如 001")
    parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--rate-hz", type=float, default=DEFAULT_RATE_HZ)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
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
    stream: av.video.stream.VideoStream | None = None
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
        if stream is not None and container is not None:
            for packet in stream.encode():
                container.mux(packet)
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
        if container is not None:
            container.close()
        channel.close()
        counts[name] = count


def main() -> int:
    args = parse_args()
    validate_args(args)
    session = f"{args.task}_{args.number}"
    output_dir = args.root.expanduser().resolve() / session
    wrist_output = output_dir / f"{args.task}_wrist_{args.number}.mp4"
    overhead_output = output_dir / f"{args.task}_overhead_{args.number}.mp4"
    metadata_output = output_dir / "preview.json"
    if output_dir.exists() and not args.overwrite:
        raise SystemExit(f"输出目录已存在：{output_dir}；需要覆盖时加 --overwrite")
    if args.overwrite and output_dir.exists():
        for path in output_dir.iterdir():
            if path.is_file():
                path.unlink()

    start_event = threading.Event()
    stop_event = threading.Event()
    errors: list[str] = []
    counts: dict[str, int] = {}
    threads = [
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
    previous_handlers = {}

    def request_stop(signum, _frame) -> None:
        print(f"收到信号 {signum}，正在正常结束 preview...", flush=True)
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)

    print(f"输出目录：{output_dir}", flush=True)
    print(f"wrist：{wrist_output.name}", flush=True)
    print(f"overhead：{overhead_output.name}", flush=True)
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

    metadata = {
        "task": args.task,
        "number": args.number,
        "duration_s": args.duration_s,
        "rate_hz": args.rate_hz,
        "frames": counts,
        "wrist_endpoint": args.wrist_endpoint,
        "overhead_endpoint": args.overhead_endpoint,
        "success": not errors and all(counts.get(name, 0) > 0 for name in ("wrist", "overhead")),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
