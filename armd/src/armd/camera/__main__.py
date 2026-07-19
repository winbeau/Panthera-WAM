"""WSL 内部 RealSense D405 采集服务。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from functools import partial

import grpc
from panthera_arm import camera_pb2, camera_pb2_grpc

from .backend import CameraWorker, RealSenseCameraBackend, SimCameraBackend
from .service import CameraService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Panthera-WAM WSL D405 采集服务")
    parser.add_argument(
        "--mode",
        choices=("auto", "sim"),
        default=os.environ.get("PANTHERA_CAMERA_MODE", "auto"),
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("PANTHERA_CAMERA_BIND", "127.0.0.1:50052"),
    )
    parser.add_argument("--serial", default=os.environ.get("PANTHERA_CAMERA_SERIAL", ""))
    parser.add_argument(
        "--width",
        type=int,
        default=int(os.environ.get("PANTHERA_CAMERA_WIDTH", "640")),
    )
    parser.add_argument(
        "--height",
        type=int,
        default=int(os.environ.get("PANTHERA_CAMERA_HEIGHT", "480")),
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=int(os.environ.get("PANTHERA_CAMERA_FPS", "30")),
    )
    parser.add_argument("--check", action="store_true", help="用仿真 D405 执行 gRPC 自检")
    return parser


async def run(args: argparse.Namespace) -> None:
    if args.check and args.mode != "sim":
        raise SystemExit("--check 必须与 --mode sim 一起使用")
    if args.width <= 0 or args.height <= 0 or args.fps <= 0:
        raise SystemExit("width/height/fps 必须为正整数")

    backend_factory = (
        partial(SimCameraBackend, width=args.width, height=args.height, fps=args.fps)
        if args.mode == "sim"
        else partial(
            RealSenseCameraBackend,
            serial=args.serial,
            width=args.width,
            height=args.height,
            fps=args.fps,
        )
    )
    worker = CameraWorker(backend_factory)
    server = grpc.aio.server()
    camera_pb2_grpc.add_CameraServiceServicer_to_server(CameraService(worker), server)
    bind = "127.0.0.1:0" if args.check else args.bind
    port = server.add_insecure_port(bind)
    if port == 0:
        raise RuntimeError(f"无法监听 gRPC 地址: {bind}")

    worker.start()
    await server.start()
    try:
        if args.check:
            async with grpc.aio.insecure_channel(
                f"127.0.0.1:{port}",
                options=(("grpc.enable_http_proxy", 0),),
            ) as channel:
                stub = camera_pb2_grpc.CameraServiceStub(channel)
                frame = await stub.CaptureFrame(
                    camera_pb2.CaptureFrameRequest(
                        stream=camera_pb2.CAMERA_STREAM_TYPE_DEPTH,
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
                            "width": frame.width,
                            "height": frame.height,
                            "bytes": len(frame.data),
                        },
                        ensure_ascii=False,
                    )
                )
            return
        print(f"camerad 已启动：grpc://{args.bind}，D405={args.mode}")
        await server.wait_for_termination()
    finally:
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
