"""Linux D405/C920e 采集服务。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from functools import partial

import grpc
from panthera_arm import camera_pb2, camera_pb2_grpc

from .backend import (
    CameraRole,
    CameraWorker,
    RealSenseCameraBackend,
    SimCameraBackend,
    SimOverheadCameraBackend,
    V4L2MjpegCameraBackend,
)
from .service import CameraService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Panthera-WAM Linux 相机采集服务")
    parser.add_argument(
        "--mode",
        choices=("auto", "sim"),
        default=os.environ.get("PANTHERA_CAMERA_MODE", "auto"),
        help="兼容参数：auto 使用角色默认后端，sim 使用仿真后端",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "realsense", "v4l2", "sim"),
        default=os.environ.get("PANTHERA_CAMERA_BACKEND", ""),
        help="相机后端；省略时由 role 和 --mode 推断",
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("PANTHERA_CAMERA_BIND", "127.0.0.1:50052"),
    )
    parser.add_argument(
        "--role",
        choices=(CameraRole.WRIST.value, CameraRole.OVERHEAD.value),
        default=os.environ.get("PANTHERA_CAMERA_ROLE", CameraRole.WRIST.value),
    )
    parser.add_argument(
        "--local-bind",
        default=os.environ.get("PANTHERA_CAMERA_LOCAL_BIND", ""),
        help="附加的 Linux 本地监听地址",
    )
    parser.add_argument("--serial", default=os.environ.get("PANTHERA_CAMERA_SERIAL", ""))
    parser.add_argument(
        "--device",
        default=os.environ.get(
            "PANTHERA_CAMERA_DEVICE",
            V4L2MjpegCameraBackend.STABLE_DEVICE_PATH,
        ),
        help="C920e 稳定设备别名",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=(int(os.environ["PANTHERA_CAMERA_WIDTH"]) if "PANTHERA_CAMERA_WIDTH" in os.environ else None),
    )
    parser.add_argument(
        "--height",
        type=int,
        default=(
            int(os.environ["PANTHERA_CAMERA_HEIGHT"]) if "PANTHERA_CAMERA_HEIGHT" in os.environ else None
        ),
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=(int(os.environ["PANTHERA_CAMERA_FPS"]) if "PANTHERA_CAMERA_FPS" in os.environ else 30),
    )
    parser.add_argument(
        "--max-frame-bytes",
        type=int,
        default=int(os.environ.get("PANTHERA_CAMERA_MAX_FRAME_BYTES", str(8 * 1024 * 1024))),
    )
    parser.add_argument("--check", action="store_true", help="用所选角色的仿真相机执行 gRPC 自检")
    return parser


async def run(args: argparse.Namespace) -> None:
    role = CameraRole(args.role)
    backend_name = args.backend or (
        "sim" if args.mode == "sim" else ("v4l2" if role is CameraRole.OVERHEAD else "realsense")
    )
    if backend_name == "auto":
        backend_name = "v4l2" if role is CameraRole.OVERHEAD else "realsense"
    if args.check and backend_name != "sim":
        raise SystemExit("--check 必须使用 --mode sim 或 --backend sim")
    width = args.width or (1920 if role is CameraRole.OVERHEAD else 640)
    height = args.height or (1080 if role is CameraRole.OVERHEAD else 480)
    if width <= 0 or height <= 0 or args.fps <= 0:
        raise SystemExit("width/height/fps 必须为正整数")
    if role is CameraRole.WRIST and backend_name not in ("realsense", "sim"):
        raise SystemExit("wrist 角色只支持 realsense/sim 后端")
    if role is CameraRole.OVERHEAD and backend_name not in ("v4l2", "sim"):
        raise SystemExit("overhead 角色只支持 v4l2/sim 后端")

    if backend_name == "sim":
        backend_factory = (
            partial(SimOverheadCameraBackend, fps=args.fps)
            if role is CameraRole.OVERHEAD
            else partial(SimCameraBackend, width=width, height=height, fps=args.fps)
        )
    elif backend_name == "realsense":
        backend_factory = partial(
            RealSenseCameraBackend,
            serial=args.serial,
            width=width,
            height=height,
            fps=args.fps,
        )
    else:
        backend_factory = partial(
            V4L2MjpegCameraBackend,
            device_path=args.device,
            width=width,
            height=height,
            fps=args.fps,
            max_frame_bytes=args.max_frame_bytes,
        )
    worker = CameraWorker(backend_factory, role=role)
    server = grpc.aio.server(
        options=(
            ("grpc.max_send_message_length", 16 * 1024 * 1024),
            ("grpc.max_receive_message_length", 16 * 1024 * 1024),
        )
    )
    camera_pb2_grpc.add_CameraServiceServicer_to_server(CameraService(worker), server)
    bind = "127.0.0.1:0" if args.check else args.bind
    port = server.add_insecure_port(bind)
    if port == 0:
        raise RuntimeError(f"无法监听 gRPC 地址: {bind}")
    for additional_bind in filter(None, (args.local_bind,)):
        if server.add_insecure_port(additional_bind) == 0:
            raise RuntimeError(f"无法监听附加 gRPC 地址: {additional_bind}")

    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    if not args.check:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_number, stop_requested.set)
                registered_signals.append(signal_number)
            except (NotImplementedError, RuntimeError):
                pass

    worker.start()
    await server.start()
    try:
        if args.check:
            async with grpc.aio.insecure_channel(
                f"127.0.0.1:{port}",
                options=(("grpc.enable_http_proxy", 0),),
            ) as channel:
                stub = camera_pb2_grpc.CameraServiceStub(channel)
                requested_stream = (
                    camera_pb2.CAMERA_STREAM_TYPE_COLOR
                    if role is CameraRole.OVERHEAD
                    else camera_pb2.CAMERA_STREAM_TYPE_DEPTH
                )
                frame = await stub.CaptureFrame(
                    camera_pb2.CaptureFrameRequest(
                        stream=requested_stream,
                        timeout_ms=2000,
                    )
                )
                status = await stub.GetStatus(camera_pb2.CameraStatusRequest())
                print(
                    json.dumps(
                        {
                            "available": status.available,
                            "streaming": status.streaming,
                            "model": status.model,
                            "role": camera_pb2.CameraDeviceRole.Name(status.role),
                            "pixel_format": camera_pb2.CameraPixelFormat.Name(frame.pixel_format),
                            "width": frame.width,
                            "height": frame.height,
                            "bytes": len(frame.data),
                            "captured_monotonic_ns": frame.captured_monotonic_ns,
                        },
                        ensure_ascii=False,
                    )
                )
            return
        binds = ", ".join(filter(None, (args.bind, args.local_bind)))
        print(
            f"camerad 已启动：grpc://{binds}，role={role.value}，backend={backend_name}",
            flush=True,
        )
        await stop_requested.wait()
    finally:
        for signal_number in registered_signals:
            loop.remove_signal_handler(signal_number)
        await server.stop(0)
        worker.stop()


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
