"""armd 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from functools import partial
from pathlib import Path

import grpc
from panthera_arm import arm_pb2, arm_pb2_grpc, camera_pb2, camera_pb2_grpc

from .backend import DEFAULT_MOTOR_TIMEOUT_MS, RealBackend, SimBackend
from .camera.backend import CameraWorker, RealSenseCameraBackend, SimCameraBackend
from .hardware_loop import HardwareLoop
from .kinematics import KinematicsEngine
from .policy import PolicySafetyConfig
from .policy_assets import PolicyAssetAllowList, PolicyAssetError
from .policy_path import ConservativePolicyPathValidator
from .server import ArmdServer


def default_sdk_root() -> str:
    configured = os.environ.get("PANTHERA_SDK_ROOT")
    if configured:
        return configured
    repository_vendor = Path(__file__).resolve().parents[3] / "vendor" / "Panthera-HT_SDK"
    if repository_vendor.is_dir():
        return str(repository_vendor)
    return str(Path.home() / "Panthera-HT_SDK")


def parse_policy_camera_boxes(
    value: str,
) -> tuple[tuple[tuple[float, float, float], tuple[float, float, float]], ...]:
    if not value:
        return ()
    try:
        records = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("PANTHERA_POLICY_CAMERA_BOXES_JSON must be valid JSON") from exc
    if not isinstance(records, list):
        raise ValueError("policy camera boxes must be a JSON list")
    result = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"policy camera box {index} must be an object")
        lower = record.get("lower")
        upper = record.get("upper")
        if not isinstance(lower, list) or not isinstance(upper, list):
            raise ValueError(f"policy camera box {index} requires lower/upper lists")
        result.append((tuple(float(item) for item in lower), tuple(float(item) for item in upper)))
    return tuple(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Panthera-HT armd 守护服务")
    parser.add_argument("--sim", action="store_true", help="使用无需真机的仿真后端")
    parser.add_argument(
        "--sdk-root",
        default=default_sdk_root(),
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
    parser.add_argument(
        "--bind",
        default=os.environ.get("PANTHERA_ARM_BIND", "127.0.0.1:50051"),
        help="gRPC 监听地址",
    )
    parser.add_argument(
        "--local-bind",
        default=os.environ.get("PANTHERA_LOCAL_BIND", ""),
        help="附加的 Linux 本地监听地址",
    )
    parser.add_argument("--lease-timeout", type=float, default=2.0, help="控制权心跳超时秒数")
    parser.add_argument(
        "--camera-mode",
        choices=("off", "auto", "proxy", "sim"),
        default=os.environ.get("PANTHERA_CAMERA_MODE") or None,
        help="D405 兼容模式：off/auto/proxy/sim（真机默认 off，由独立 camerad 提供）",
    )
    parser.add_argument(
        "--camera-endpoint",
        default=os.environ.get("PANTHERA_CAMERA_ENDPOINT", "127.0.0.1:50052"),
        help="proxy 模式的 Linux camerad 端点",
    )
    parser.add_argument(
        "--camera-serial",
        default=os.environ.get("PANTHERA_CAMERA_SERIAL", ""),
        help="指定 D405 序列号；空值自动选择首台 D405",
    )
    parser.add_argument(
        "--camera-width",
        type=int,
        default=int(os.environ.get("PANTHERA_CAMERA_WIDTH", "640")),
    )
    parser.add_argument(
        "--camera-height",
        type=int,
        default=int(os.environ.get("PANTHERA_CAMERA_HEIGHT", "480")),
    )
    parser.add_argument(
        "--camera-fps",
        type=int,
        default=int(os.environ.get("PANTHERA_CAMERA_FPS", "30")),
    )
    parser.add_argument(
        "--policy-table-z-min",
        type=float,
        default=(
            float(os.environ["PANTHERA_POLICY_TABLE_Z_MIN"])
            if os.environ.get("PANTHERA_POLICY_TABLE_Z_MIN")
            else None
        ),
        help="启用真机 policy 时 tool_link 最低允许 base-Z；未设则真机 policy fail-closed",
    )
    parser.add_argument(
        "--policy-base-radius",
        type=float,
        default=float(os.environ.get("PANTHERA_POLICY_BASE_RADIUS", "0.1")),
        help="tool_link 相对 base 原点的 XY 禁入圆柱半径（米）",
    )
    parser.add_argument(
        "--policy-camera-boxes-json",
        default=os.environ.get("PANTHERA_POLICY_CAMERA_BOXES_JSON", ""),
        help="已标定相机/支架 XYZ 禁入盒 JSON；真机 policy 至少要求一个",
    )
    parser.add_argument(
        "--policy-confirmation-socket",
        default=os.environ.get(
            "PANTHERA_POLICY_CONFIRMATION_SOCKET",
            f"/run/user/{os.getuid()}/panthera-policy-confirm.sock",
        ),
        help="Pi 本地操作员确认 Unix socket；不暴露到 gRPC/Tailscale",
    )
    parser.add_argument(
        "--policy-asset-allow-list",
        default=os.environ.get("PANTHERA_POLICY_ASSET_ALLOW_LIST", ""),
        help="真机 policy checkpoint/stats/schema 精确 allow-list JSON；未设则真机 policy fail-closed",
    )
    parser.add_argument(
        "--policy-start-measurement-tolerance",
        type=float,
        default=float(os.environ.get("PANTHERA_POLICY_START_MEASUREMENT_TOLERANCE", "0.01")),
        help="启动读数落在软限位外时允许的测量偏差（关节坐标；默认 0.01）",
    )
    parser.add_argument(
        "--policy-endpoint-tolerance-m",
        type=float,
        default=float(os.environ.get("PANTHERA_POLICY_ENDPOINT_TOLERANCE_M", "0.03")),
        help="真机实验末端到位验收容差（米；默认 0.03）",
    )
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
    if args.camera_width <= 0 or args.camera_height <= 0 or args.camera_fps <= 0:
        raise SystemExit("camera width/height/fps 必须为正整数")
    camera_mode = args.camera_mode or ("sim" if args.sim else "off")
    if args.sim and camera_mode in ("auto", "proxy"):
        camera_mode = "sim"
    camera_worker = None
    if camera_mode == "sim":
        camera_worker = CameraWorker(
            partial(
                SimCameraBackend,
                width=args.camera_width,
                height=args.camera_height,
                fps=args.camera_fps,
            )
        )
    elif camera_mode == "auto":
        camera_worker = CameraWorker(
            partial(
                RealSenseCameraBackend,
                serial=args.camera_serial,
                width=args.camera_width,
                height=args.camera_height,
                fps=args.camera_fps,
            )
        )
    camera_endpoint = args.camera_endpoint if camera_mode == "proxy" else None
    bind = "127.0.0.1:0" if args.check else args.bind
    try:
        policy_camera_boxes = parse_policy_camera_boxes(args.policy_camera_boxes_json)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    policy_path_validator = None
    if not args.sim and args.policy_table_z_min is not None and policy_camera_boxes:
        policy_kinematics = KinematicsEngine(
            sdk_root=args.sdk_root,
            config_path=args.config,
        )
        policy_path_validator = ConservativePolicyPathValidator(
            forward_kinematics=lambda joints: policy_kinematics.forward_kinematics(joints)["position"],
            table_z_min=args.policy_table_z_min,
            base_radius_m=args.policy_base_radius,
            camera_boxes=policy_camera_boxes,
            require_camera_boxes=True,
        )
    policy_config = PolicySafetyConfig(
        measured_limit_start_tolerance=args.policy_start_measurement_tolerance,
        hardware_endpoint_tolerance_m=args.policy_endpoint_tolerance_m,
    )
    policy_asset_allow_list = None
    if args.policy_asset_allow_list:
        try:
            policy_asset_allow_list = PolicyAssetAllowList.load(args.policy_asset_allow_list)
        except PolicyAssetError as exc:
            raise SystemExit(str(exc)) from exc
    server = ArmdServer(
        loop,
        bind=bind,
        lease_timeout_s=args.lease_timeout,
        sdk_root=args.sdk_root,
        config_path=args.config,
        camera_worker=camera_worker,
        camera_endpoint=camera_endpoint,
        additional_binds=(args.local_bind,) if args.local_bind else (),
        policy_config=policy_config,
        policy_path_validator=policy_path_validator,
        policy_confirmation_socket=(args.policy_confirmation_socket if not args.sim else None),
        policy_asset_allow_list=policy_asset_allow_list,
    )
    loop.start()
    try:
        await server.start()
        if args.check:
            if not loop.wait_for_cycles(3):
                raise SystemExit("仿真控制循环未能按期推进")
            async with grpc.aio.insecure_channel(
                f"127.0.0.1:{server.port}",
                options=(("grpc.enable_http_proxy", 0),),
            ) as channel:
                stub = arm_pb2_grpc.ArmServiceStub(channel)
                camera_stub = camera_pb2_grpc.CameraServiceStub(channel)
                status = await stub.GetDaemonStatus(arm_pb2.Empty())
                camera_status = await camera_stub.GetStatus(camera_pb2.CameraStatusRequest())
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
                            "camera_enabled": camera_status.enabled,
                            "camera_available": camera_status.available,
                        },
                        ensure_ascii=False,
                    )
                )
            return

        mode = "仿真" if args.sim else f"真机（固件看门狗 {args.motor_timeout_ms}ms）"
        binds = ", ".join(filter(None, (args.bind, args.local_bind)))
        print(
            f"armd {mode}服务已启动：grpc://{binds}，HardwareLoop={args.control_hz:g}Hz，D405={camera_mode}"
        )
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
