"""armd 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from functools import partial
from pathlib import Path

import grpc
import numpy as np
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


def parse_gravity_scale(value: str) -> np.ndarray | float:
    """解析 1 个标量或逗号分隔 6 个值的重力补偿标定系数。"""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("teach-gravity-scale 不能为空")
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"teach-gravity-scale 含非法数值: {value}") from exc
    if len(values) == 1:
        return float(values[0])
    if len(values) != 6:
        raise argparse.ArgumentTypeError("teach-gravity-scale 必须为 1 个或 6 个数值")
    return np.asarray(values, dtype=np.float64)


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
    parser.add_argument(
        "--allow-unverified-teach",
        action="store_true",
        default=os.environ.get("PANTHERA_ALLOW_UNVERIFIED_TEACH", "0") == "1",
        help="显式放行未验证坐标契约的真实 MIT teach/playback（默认拒绝，安全门控）",
    )
    parser.add_argument(
        "--teach-gravity-scale",
        type=parse_gravity_scale,
        default=parse_gravity_scale(os.environ.get("PANTHERA_TEACH_GRAVITY_SCALE", "1.0")),
        help="示教重力补偿标定系数：1 个值（全部关节）或逗号分隔 6 个值（逐关节）；见 docs/JOINT_CONTROL.md",
    )
    parser.add_argument(
        "--teach-gravity-scale-high",
        type=parse_gravity_scale,
        default=(
            parse_gravity_scale(os.environ["PANTHERA_TEACH_GRAVITY_SCALE_HIGH"])
            if os.environ.get("PANTHERA_TEACH_GRAVITY_SCALE_HIGH")
            else None
        ),
        help="示教重力补偿高区系数（q>断点后使用；默认=低区=不分段）",
    )
    parser.add_argument(
        "--teach-gravity-breakpoint",
        type=parse_gravity_scale,
        default=(
            parse_gravity_scale(os.environ["PANTHERA_TEACH_GRAVITY_BREAKPOINT"])
            if os.environ.get("PANTHERA_TEACH_GRAVITY_BREAKPOINT")
            else None
        ),
        help="示教重力补偿分段断点（rad；仅 --teach-gravity-segmented 开启后生效）",
    )
    parser.add_argument(
        "--teach-gravity-segmented",
        action="store_true",
        default=os.environ.get("PANTHERA_TEACH_GRAVITY_SEGMENTED", "0") == "1",
        help="显式启用分段重力补偿；默认关闭，避免固定断点产生人工吸附点",
    )
    parser.add_argument(
        "--teach-gravity-residual",
        type=parse_gravity_scale,
        default=parse_gravity_scale(os.environ.get("PANTHERA_TEACH_GRAVITY_RESIDUAL", "0.0")),
        help="连续逐关节重力残差偏置（Nm；1个或6个值，默认全零）",
    )
    parser.add_argument(
        "--teach-auto-hold",
        action="store_true",
        default=os.environ.get("PANTHERA_TEACH_AUTO_HOLD", "1") == "1",
        help="示教 Auto-Hold 静止自动锁位（默认启用；关闭用 --no-teach-auto-hold）",
    )
    parser.add_argument(
        "--no-teach-auto-hold",
        action="store_false",
        dest="teach_auto_hold",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--teach-manual-clutch",
        action="store_true",
        default=os.environ.get("PANTHERA_TEACH_MANUAL_CLUTCH", "0") == "1",
        help="启用 teach 显式 lock/drag 离合命令（默认关闭）",
    )
    parser.add_argument(
        "--teach-safe-hold-s",
        type=float,
        default=float(os.environ.get("PANTHERA_TEACH_SAFE_HOLD_S", "10.0")),
        help="显式离合 teach 取消后的安全保持时长（秒；期间保持重力前馈与位置刚度）",
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
        allow_unverified_teach=args.allow_unverified_teach,
        teach_gravity_scale=args.teach_gravity_scale,
        teach_gravity_scale_high=args.teach_gravity_scale_high,
        teach_gravity_breakpoint=args.teach_gravity_breakpoint,
        teach_gravity_segmented=args.teach_gravity_segmented,
        teach_gravity_residual=args.teach_gravity_residual,
        auto_hold_enabled=args.teach_auto_hold,
        teach_manual_clutch=args.teach_manual_clutch,
        teach_safe_hold_s=args.teach_safe_hold_s,
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
        if not args.sim and not args.allow_unverified_teach:
            print(
                "安全门控：真实 MIT teach/playback 已默认拒绝（坐标契约未验证）；"
                "核对 docs/COORDINATE_CONTRACT.md 后可用 --allow-unverified-teach 放行"
            )
        print(
            f"armd {mode}服务已启动：grpc://{binds}，HardwareLoop={args.control_hz:g}Hz，D405={camera_mode}"
        )
        await server.wait_for_termination()
    finally:
        await server.stop()
        loop.stop()


def main() -> None:
    # 日志直通 journal：teach 状态机 / 运动异常等 logger 输出对真机诊断至关重要
    # （此前未配置 basicConfig，logger.* 全部丢失，end-lock teach 瞬死无迹可查）。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
