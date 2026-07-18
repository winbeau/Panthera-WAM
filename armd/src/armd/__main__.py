"""armd 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import grpc
from panthera_arm import arm_pb2, arm_pb2_grpc

from .backend import DEFAULT_MOTOR_TIMEOUT_MS, RealBackend, SimBackend
from .hardware_loop import HardwareLoop
from .server import ArmdServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Panthera-HT armd 守护服务")
    parser.add_argument("--sim", action="store_true", help="使用无需真机的仿真后端")
    parser.add_argument(
        "--sdk-root",
        default=os.environ.get("PANTHERA_SDK_ROOT", str(Path.home() / "Panthera-HT_SDK")),
        help="官方 Panthera-HT_SDK 根目录",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("PANTHERA_CONFIG") or None,
        help="官方 SDK Follower.yaml 路径；省略则使用 SDK 默认配置",
    )
    parser.add_argument(
        "--motor-timeout-ms",
        type=int,
        default=int(os.environ.get("PANTHERA_MOTOR_TIMEOUT_MS", DEFAULT_MOTOR_TIMEOUT_MS)),
        help="电机固件看门狗毫秒数（默认 150；0 表示禁用）",
    )
    parser.add_argument("--control-hz", type=float, default=200.0, help="控制循环频率（默认 200Hz）")
    parser.add_argument("--bind", default="127.0.0.1:50051", help="gRPC 监听地址")
    parser.add_argument("--lease-timeout", type=float, default=2.0, help="控制权心跳超时秒数")
    parser.add_argument("--check", action="store_true", help="启动后通过 gRPC 做一次仿真自检并退出")
    return parser


async def run(args: argparse.Namespace) -> None:
    if args.check and not args.sim:
        raise SystemExit("--check 仅用于仿真；真机请启动 armd 后通过 daemon status 验收")

    if args.sim:
        backend_factory = SimBackend
    else:

        def backend_factory() -> RealBackend:
            return RealBackend(
                sdk_root=args.sdk_root,
                config_path=args.config,
                motor_timeout_ms=args.motor_timeout_ms,
            )

    loop = HardwareLoop(backend_factory, control_hz=args.control_hz)
    bind = "127.0.0.1:0" if args.check else args.bind
    server = ArmdServer(loop, bind=bind, lease_timeout_s=args.lease_timeout)
    loop.start()
    try:
        await server.start()
        if args.check:
            if not loop.wait_for_cycles(3):
                raise SystemExit("仿真控制循环未能按期推进")
            async with grpc.aio.insecure_channel(f"127.0.0.1:{server.port}") as channel:
                stub = arm_pb2_grpc.ArmServiceStub(channel)
                status = await stub.GetDaemonStatus(arm_pb2.Empty())
                stats = loop.stats()
                print(
                    json.dumps(
                        {
                            "sim": status.sim,
                            "hardware_connected": status.hardware_connected,
                            "grpc_port": server.port,
                            "cycles": stats.cycles,
                            "actual_hz": round(stats.actual_hz, 2),
                            "overruns": stats.overruns,
                        },
                        ensure_ascii=False,
                    )
                )
            return

        mode = "仿真" if args.sim else f"真机（固件看门狗 {args.motor_timeout_ms}ms）"
        print(f"armd {mode}服务已启动：grpc://{args.bind}，HardwareLoop={args.control_hz:g}Hz")
        await server.wait_for_termination()
    finally:
        await server.stop()
        loop.stop()


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
