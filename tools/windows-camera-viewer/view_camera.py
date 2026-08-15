"""Simple Windows viewer for a Panthera Pi camera gRPC stream.

Examples:
    py view_camera.py --source overhead
    py view_camera.py --source wrist
    py view_camera.py --source wrist --stream depth

Press q or Esc to close the video window.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

import cv2
import grpc
import numpy as np

from proto import camera_pb2, camera_pb2_grpc

DEFAULT_HOST = "100.78.118.74"
DEFAULT_PORTS = {"wrist": 50052, "overhead": 50053}
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Panthera Pi live camera viewer")
    parser.add_argument(
        "--source",
        choices=("wrist", "overhead"),
        default="overhead",
        help="camera to view (default: overhead)",
    )
    parser.add_argument(
        "--stream",
        choices=("color", "depth"),
        default="color",
        help="color or depth stream (default: color)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Pi address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, help="override the camera gRPC port")
    parser.add_argument("--rate", type=float, default=30.0, help="requested FPS, 0.1-90 (default: 30)")
    parser.add_argument("--depth-max-mm", type=float, default=3000.0, help="depth color-map range (default: 3000)")
    return parser.parse_args()


def stream_value(name: str) -> int:
    if name == "color":
        return camera_pb2.CAMERA_STREAM_TYPE_COLOR
    return camera_pb2.CAMERA_STREAM_TYPE_DEPTH


def decode_frame(frame: Any, depth_max_mm: float) -> np.ndarray | None:
    """Convert a CameraFrame protobuf message into an OpenCV BGR image."""
    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_JPEG:
        encoded = np.frombuffer(frame.data, dtype=np.uint8)
        return cv2.imdecode(encoded, cv2.IMREAD_COLOR)

    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_RGB8:
        stride = frame.stride or frame.width * 3
        raw = np.frombuffer(frame.data, dtype=np.uint8)
        needed = frame.height * stride
        if raw.size < needed:
            return None
        rgb = raw[:needed].reshape(frame.height, stride)[:, : frame.width * 3]
        rgb = rgb.reshape(frame.height, frame.width, 3)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_Z16:
        stride = frame.stride or frame.width * 2
        row_pixels = stride // 2
        raw = np.frombuffer(frame.data, dtype="<u2")
        needed = frame.height * row_pixels
        if raw.size < needed:
            return None
        depth = raw[:needed].reshape(frame.height, row_pixels)[:, : frame.width]
        scale = 255.0 / max(depth_max_mm, 1.0)
        depth8 = np.clip(depth.astype(np.float32) * float(frame.depth_scale) * 1000.0 * scale, 0, 255).astype(np.uint8)
        return cv2.applyColorMap(depth8, cv2.COLORMAP_TURBO)

    return None


def main() -> int:
    args = parse_args()
    if args.source == "overhead" and args.stream == "depth":
        print("overhead camera has no depth stream", file=sys.stderr)
        return 2
    if not 0.1 <= args.rate <= 90.0:
        print("--rate must be between 0.1 and 90", file=sys.stderr)
        return 2

    port = args.port or DEFAULT_PORTS[args.source]
    endpoint = f"{args.host}:{port}"
    options = [
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ]
    channel = grpc.insecure_channel(endpoint, options=options)
    stub = camera_pb2_grpc.CameraServiceStub(channel)
    try:
        status = stub.GetStatus(camera_pb2.CameraStatusRequest(), timeout=5.0)
        if not status.available or not status.streaming:
            print(f"camera unavailable: {status.error or 'not streaming'}", file=sys.stderr)
            return 1
        print(
            f"connected: {endpoint} | {args.source}/{args.stream} | "
            f"{status.model or 'camera'} | actual_fps={status.actual_fps:.1f}",
            flush=True,
        )

        request = camera_pb2.StreamFramesRequest(
            stream=stream_value(args.stream),
            max_rate_hz=args.rate,
            max_frames=0,
        )
        title = f"Panthera {args.source} - {args.stream}"
        cv2.namedWindow(title, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(title, 1280, 720)
        for frame in stub.StreamFrames(request):
            image = decode_frame(frame, args.depth_max_mm)
            if image is None:
                continue
            cv2.putText(
                image,
                f"{args.source}/{args.stream}  seq={frame.sequence}  q/Esc=quit",
                (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
            cv2.imshow(title, image)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as exc:
        print(f"gRPC error: {exc.code().name}: {exc.details()}", file=sys.stderr)
        return 1
    finally:
        channel.close()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
