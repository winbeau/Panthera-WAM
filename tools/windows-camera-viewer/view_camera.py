"""Simple Windows viewer for a Panthera Pi camera gRPC stream.

Default mode uses the SSH alias ``pi5`` from the user's OpenSSH config and
creates a local tunnel automatically:

    py view_camera.py --source overhead
    py view_camera.py --source wrist

Direct mode is also available when the Pi address is reachable directly:

    py view_camera.py --source overhead --host 192.168.10.249

Press q or Esc to close the video window.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import grpc
import numpy as np

from proto import camera_pb2, camera_pb2_grpc

DEFAULT_SSH_ALIAS = "pi5"
DEFAULT_PORTS = {"wrist": 50052, "overhead": 50053}
DEFAULT_LOCAL_PORTS = {"wrist": 15052, "overhead": 15053}
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
    parser.add_argument(
        "--ssh-alias",
        default=DEFAULT_SSH_ALIAS,
        help=f"OpenSSH alias used for the tunnel (default: {DEFAULT_SSH_ALIAS})",
    )
    parser.add_argument(
        "--ssh-config",
        type=Path,
        default=Path.home() / ".ssh" / "config",
        help="OpenSSH config path (default: %%USERPROFILE%%\\.ssh\\config)",
    )
    parser.add_argument(
        "--host",
        help="direct gRPC host; when set, SSH tunneling is disabled",
    )
    parser.add_argument("--port", type=int, help="override the remote camera gRPC port")
    parser.add_argument(
        "--local-port",
        type=int,
        help="local tunnel port (default: 15052 wrist / 15053 overhead)",
    )
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
        depth8 = np.clip(
            depth.astype(np.float32) * float(frame.depth_scale) * 1000.0 * scale,
            0,
            255,
        ).astype(np.uint8)
        return cv2.applyColorMap(depth8, cv2.COLORMAP_TURBO)

    return None


def wait_for_local_port(process: subprocess.Popen[str], port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = ""
            if process.stderr is not None:
                detail = process.stderr.read().strip()
            raise RuntimeError(f"ssh tunnel exited with code {process.returncode}: {detail}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"SSH tunnel did not open local port {port}")


def start_ssh_tunnel(
    alias: str,
    config: Path,
    remote_port: int,
    local_port: int,
) -> subprocess.Popen[str]:
    ssh = "ssh.exe" if sys.platform == "win32" else "ssh"
    command = [
        ssh,
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=8",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
    ]
    if config.exists():
        command.extend(["-F", str(config)])
    command.append(alias)
    try:
        process = subprocess.Popen(
            command,
            stdin=None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ssh.exe，请先启用 Windows OpenSSH Client") from exc
    wait_for_local_port(process, local_port)
    return process


def stop_ssh_tunnel(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def main() -> int:
    args = parse_args()
    if args.source == "overhead" and args.stream == "depth":
        print("overhead camera has no depth stream", file=sys.stderr)
        return 2
    if not 0.1 <= args.rate <= 90.0:
        print("--rate must be between 0.1 and 90", file=sys.stderr)
        return 2

    remote_port = args.port or DEFAULT_PORTS[args.source]
    tunnel: subprocess.Popen[str] | None = None
    try:
        if args.host:
            endpoint = f"{args.host}:{remote_port}"
            connection_label = f"direct {endpoint}"
        else:
            local_port = args.local_port or DEFAULT_LOCAL_PORTS[args.source]
            tunnel = start_ssh_tunnel(args.ssh_alias, args.ssh_config.expanduser(), remote_port, local_port)
            endpoint = f"127.0.0.1:{local_port}"
            connection_label = f"ssh {args.ssh_alias} -> {endpoint}"

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
                f"connected: {connection_label} | {args.source}/{args.stream} | "
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
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"connection error: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_ssh_tunnel(tunnel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
