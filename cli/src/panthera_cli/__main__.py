"""panthera-cli M1 命令。"""

from __future__ import annotations

import json
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import grpc
import typer
from panthera_arm import arm_pb2, camera_pb2
from rich.console import Console
from rich.table import Table

from .client import (
    SavedLease,
    clear_lease,
    create_camera_stub,
    create_stub,
    default_client_id,
    endpoint,
    lease_metadata,
    load_lease,
    maintain_heartbeat,
    save_lease,
)

app = typer.Typer(no_args_is_help=True, help="Panthera-HT armd 命令行客户端")
control_app = typer.Typer(no_args_is_help=True)
estop_app = typer.Typer(no_args_is_help=True)
safety_app = typer.Typer(no_args_is_help=True)
limits_app = typer.Typer(no_args_is_help=True)
daemon_app = typer.Typer(no_args_is_help=True)
state_app = typer.Typer(no_args_is_help=True)
calibrate_app = typer.Typer(no_args_is_help=True)
joint_app = typer.Typer(no_args_is_help=True)
gripper_app = typer.Typer(no_args_is_help=True)
kinematics_app = typer.Typer(no_args_is_help=True)
cartesian_app = typer.Typer(no_args_is_help=True)
execution_app = typer.Typer(no_args_is_help=True)
camera_app = typer.Typer(no_args_is_help=True)
dynamics_app = typer.Typer(no_args_is_help=True)
trajectory_app = typer.Typer(no_args_is_help=True)
app.add_typer(control_app, name="control")
app.add_typer(estop_app, name="estop")
app.add_typer(safety_app, name="safety")
app.add_typer(daemon_app, name="daemon")
app.add_typer(state_app, name="state")
app.add_typer(calibrate_app, name="calibrate")
app.add_typer(joint_app, name="joint")
app.add_typer(gripper_app, name="gripper")
app.add_typer(kinematics_app, name="kinematics")
app.add_typer(cartesian_app, name="cartesian")
app.add_typer(execution_app, name="execution")
app.add_typer(camera_app, name="camera")
app.add_typer(dynamics_app, name="dynamics")
app.add_typer(trajectory_app, name="trajectory")
safety_app.add_typer(limits_app, name="limits")
console = Console()


def float_list(value: str, *, name: str, length: int = 6) -> list[float]:
    try:
        values = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise typer.BadParameter(f"{name} 必须是逗号分隔数值") from exc
    if len(values) != length:
        raise typer.BadParameter(f"{name} 必须包含 {length} 个数值")
    return values


def optional_float_list(value: str | None, *, name: str, length: int = 6) -> list[float]:
    return [] if value is None else float_list(value, name=name, length=length)


def motor_data(motor) -> dict:
    return {
        "name": motor.name,
        "motor_id": motor.motor_id,
        "position": motor.position,
        "velocity": motor.velocity,
        "torque": motor.torque,
        "motor_time": motor.motor_time,
        "mode": motor.mode,
        "fault": motor.fault,
        "pos_limit_flag": motor.pos_limit_flag,
        "tor_limit_flag": motor.tor_limit_flag,
        "valid": motor.valid,
    }


def print_motor_table(items: list[dict]) -> None:
    table = Table("电机", "ID", "位置", "速度", "力矩", "模式", "故障", "有效")
    for item in items:
        table.add_row(
            item["name"],
            str(item["motor_id"]),
            f"{item['position']:.4f}",
            f"{item['velocity']:.4f}",
            f"{item['torque']:.3f}",
            f"0x{item['mode']:02X}",
            str(item["fault"]),
            "yes" if item["valid"] else "no",
        )
    console.print(table)


def cartesian_pose(position: str, rpy: str | None) -> arm_pb2.CartesianPose:
    pose = arm_pb2.CartesianPose(position=float_list(position, name="pos", length=3))
    if rpy is not None:
        values = float_list(rpy, name="rpy", length=3)
        pose.rpy.CopyFrom(arm_pb2.RPY(roll=values[0], pitch=values[1], yaw=values[2]))
    return pose


def fail_rpc(exc: grpc.RpcError) -> None:
    detail = exc.details() if hasattr(exc, "details") else str(exc)
    code = exc.code().name if hasattr(exc, "code") else "RPC_ERROR"
    console.print(f"[red]{code}[/red]: {detail}")
    raise typer.Exit(1)


def camera_stream_type(value: str) -> int:
    normalized = value.strip().lower()
    if normalized == "depth":
        return camera_pb2.CAMERA_STREAM_TYPE_DEPTH
    if normalized == "color":
        return camera_pb2.CAMERA_STREAM_TYPE_COLOR
    raise typer.BadParameter("stream 必须是 depth 或 color")


def camera_status_data(status) -> dict:
    return {
        "enabled": status.enabled,
        "available": status.available,
        "streaming": status.streaming,
        "model": status.model,
        "serial": status.serial,
        "firmware": status.firmware,
        "usb_type": status.usb_type,
        "sdk_version": status.sdk_version,
        "error": status.error,
        "last_frame_age_ms": status.last_frame_age_ms,
        "actual_fps": status.actual_fps,
        "profiles": [
            {
                "stream": camera_pb2.CameraStreamType.Name(profile.stream),
                "pixel_format": camera_pb2.CameraPixelFormat.Name(profile.pixel_format),
                "width": profile.width,
                "height": profile.height,
                "fps": profile.fps,
            }
            for profile in status.profiles
        ],
    }


def save_camera_frame(frame, output: Path) -> Path:
    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_Z16:
        output = output.with_suffix(output.suffix or ".pgm")
        payload = bytearray(frame.data)
        if sys.byteorder == "little":
            for index in range(0, len(payload), 2):
                payload[index], payload[index + 1] = payload[index + 1], payload[index]
        header = f"P5\n{frame.width} {frame.height}\n65535\n".encode()
    elif frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_RGB8:
        output = output.with_suffix(output.suffix or ".ppm")
        payload = frame.data
        header = f"P6\n{frame.width} {frame.height}\n255\n".encode()
    else:
        output = output.with_suffix(output.suffix or ".raw")
        payload = frame.data
        header = b""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + payload)
    metadata = {
        "sequence": frame.sequence,
        "captured_at_ns": frame.captured_at_ns,
        "device_timestamp_ms": frame.device_timestamp_ms,
        "width": frame.width,
        "height": frame.height,
        "stride": frame.stride,
        "depth_scale": frame.depth_scale,
        "pixel_format": camera_pb2.CameraPixelFormat.Name(frame.pixel_format),
        "image": output.name,
    }
    output.with_suffix(output.suffix + ".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


def dynamics_request(
    term: int,
    *,
    joint_angles: str | None = None,
    joint_vel: str | None = None,
    accel: str | None = None,
    fc: str | None = None,
    fv: str | None = None,
    vel_threshold: float | None = None,
) -> arm_pb2.DynamicsQueryRequest:
    request = arm_pb2.DynamicsQueryRequest(
        term=term,
        q=optional_float_list(joint_angles, name="joint-angles"),
        v=optional_float_list(joint_vel, name="joint-vel"),
        a=optional_float_list(accel, name="accel"),
        fc=optional_float_list(fc, name="fc"),
        fv=optional_float_list(fv, name="fv"),
    )
    if vel_threshold is not None:
        request.vel_threshold = vel_threshold
    return request


def run_dynamics(request: arm_pb2.DynamicsQueryRequest, *, as_json: bool) -> None:
    channel, stub = create_stub()
    try:
        response = stub.GetDynamicsTerm(request)
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    data = {
        "gravity": list(response.gravity),
        "coriolis_matrix": list(response.coriolis_matrix),
        "coriolis_vector": list(response.coriolis_vector),
        "mass_matrix": list(response.mass_matrix),
        "inertia_terms": list(response.inertia_terms),
        "inverse_dynamics": list(response.inverse_dynamics),
        "friction_compensation": list(response.friction_compensation),
    }
    data = {key: value for key, value in data.items() if value}
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
    else:
        console.print(data)


@control_app.command("acquire")
def acquire_control(
    client_id: str = typer.Option(default_client_id(), "--client-id"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    channel, stub = create_stub()
    try:
        response = stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id=client_id, force=force))
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if not response.granted:
        console.print(f"[yellow]控制权被占用[/yellow]: {response.holder_client_id}")
        raise typer.Exit(2)
    save_lease(SavedLease(endpoint(), client_id, response.lease_token))
    console.print(f"[green]已获取控制权[/green]: {client_id}")


@control_app.command("release")
def release_control() -> None:
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)
    try:
        stub.ReleaseControl(arm_pb2.Empty(), metadata=lease_metadata(lease))
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    clear_lease()
    console.print("[green]控制权已释放[/green]")


@control_app.command("status")
def control_status(as_json: bool = typer.Option(False, "--json")) -> None:
    channel, stub = create_stub()
    try:
        status = stub.GetControlStatus(arm_pb2.Empty())
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    data = {
        "held": status.held,
        "holder_client_id": status.holder_client_id,
        "estop_engaged": status.estop_engaged,
        "watchdog_ok": status.watchdog_ok,
        "last_heartbeat_age_ms": status.last_heartbeat_age_ms,
    }
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
        return
    console.print(data)


@control_app.command("heartbeat")
def heartbeat(interval: float = typer.Option(0.5, min=0.05)) -> None:
    """前台维持当前 lease；Ctrl+C 停止。"""
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)

    def requests():
        while True:
            yield arm_pb2.HeartbeatRequest()
            time.sleep(interval)

    console.print(f"正在维持 {lease.client_id} 的 lease（Ctrl+C 停止）")
    try:
        for response in stub.Heartbeat(requests(), metadata=lease_metadata(lease)):
            if not response.ok:
                raise RuntimeError("服务端拒绝 heartbeat")
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()


@estop_app.command("trigger")
def trigger_estop(reason: str = typer.Option("panthera-cli", "--reason")) -> None:
    channel, stub = create_stub()
    try:
        response = stub.EStop(arm_pb2.EStopRequest(reason=reason))
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"[red]EStop 已触发[/red] timestamp_ms={response.timestamp_ms}")


@estop_app.command("reset")
def reset_estop(confirm: bool = typer.Option(False, "--confirm")) -> None:
    if not confirm:
        console.print("[red]必须显式传入 --confirm[/red]")
        raise typer.Exit(2)
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)
    try:
        response = stub.ClearEStop(
            arm_pb2.ClearEStopRequest(confirm=True),
            metadata=lease_metadata(lease),
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"[green]EStop 已复位[/green] engaged={response.engaged}")


@limits_app.command("show")
def show_limits(as_json: bool = typer.Option(False, "--json")) -> None:
    channel, stub = create_stub()
    try:
        limits = stub.GetSoftLimits(arm_pb2.Empty())
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    data = [
        {
            "name": item.name,
            "min": item.pos_min,
            "max": item.pos_max,
            "velocity": item.vel_max,
            "torque": item.torque_max,
        }
        for item in limits.joint_limits
    ]
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
        return
    table = Table("关节", "下限", "上限", "速度", "力矩")
    for item in data:
        table.add_row(
            item["name"],
            f"{item['min']:.3f}",
            f"{item['max']:.3f}",
            f"{item['velocity']:.3f}",
            f"{item['torque']:.1f}",
        )
    console.print(table)


@safety_app.command("check-reached")
def check_reached(
    target_positions: str = typer.Option(..., "--pos"),
    tolerance: float = typer.Option(0.1, "--tolerance", min=0.0),
) -> None:
    channel, stub = create_stub()
    try:
        response = stub.CheckReached(
            arm_pb2.CheckReachedRequest(
                target_positions=float_list(target_positions, name="pos"),
                tolerance=tolerance,
            )
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"reached={response.reached} errors={[round(value, 6) for value in response.errors]}")


@daemon_app.command("status")
def daemon_status(as_json: bool = typer.Option(False, "--json")) -> None:
    channel, stub = create_stub()
    try:
        status = stub.GetDaemonStatus(arm_pb2.Empty())
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    data = {
        "version": status.version,
        "sim": status.sim,
        "control_hz": status.control_hz,
        "uptime_ms": status.uptime_ms,
        "sdk_version": status.sdk_version,
        "hardware_connected": status.hardware_connected,
    }
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
        return
    console.print(data)


@daemon_app.command("version")
def daemon_version() -> None:
    daemon_status(as_json=False)


@camera_app.command("status")
def camera_status(as_json: bool = typer.Option(False, "--json")) -> None:
    channel, stub = create_camera_stub()
    try:
        status = stub.GetStatus(camera_pb2.CameraStatusRequest())
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    data = camera_status_data(status)
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
    else:
        console.print(data)
    if not status.available or not status.streaming:
        raise typer.Exit(2)


@camera_app.command("snapshot")
def camera_snapshot(
    stream: str = typer.Option("depth", "--stream"),
    output: Path | None = typer.Option(None, "--out"),
    timeout_ms: int = typer.Option(5000, "--timeout-ms", min=100, max=10000),
) -> None:
    stream_type = camera_stream_type(stream)
    channel, stub = create_camera_stub()
    try:
        frame = stub.CaptureFrame(camera_pb2.CaptureFrameRequest(stream=stream_type, timeout_ms=timeout_ms))
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    default_name = f"d405-{stream}-{frame.sequence}"
    saved = save_camera_frame(frame, output or Path(default_name))
    console.print(f"[green]已保存[/green] {saved} ({frame.width}x{frame.height}, sequence={frame.sequence})")


@camera_app.command("stream")
def camera_stream(
    stream: str = typer.Option("depth", "--stream"),
    max_rate_hz: float = typer.Option(10.0, "--rate-hz", min=0.1, max=90.0),
    frames: int = typer.Option(30, "--frames", min=0),
    output_dir: Path | None = typer.Option(None, "--out-dir"),
) -> None:
    stream_type = camera_stream_type(stream)
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    channel, stub = create_camera_stub()
    received = 0
    try:
        for frame in stub.StreamFrames(
            camera_pb2.StreamFramesRequest(
                stream=stream_type,
                max_rate_hz=max_rate_hz,
                max_frames=frames,
            )
        ):
            received += 1
            if output_dir is not None:
                saved = save_camera_frame(frame, output_dir / f"{stream}-{frame.sequence:08d}")
                console.print(f"{frame.sequence}: {saved}")
            else:
                console.print(
                    f"sequence={frame.sequence} {frame.width}x{frame.height} "
                    f"device_ts={frame.device_timestamp_ms:.3f}ms bytes={len(frame.data)}"
                )
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"received={received}")


@dynamics_app.command("gravity")
def dynamics_gravity(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run_dynamics(
        dynamics_request(arm_pb2.DYNAMICS_TERM_GRAVITY, joint_angles=joint_angles),
        as_json=as_json,
    )


@dynamics_app.command("coriolis")
def dynamics_coriolis(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
    joint_vel: str | None = typer.Option(None, "--joint-vel"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run_dynamics(
        dynamics_request(
            arm_pb2.DYNAMICS_TERM_CORIOLIS,
            joint_angles=joint_angles,
            joint_vel=joint_vel,
        ),
        as_json=as_json,
    )


@dynamics_app.command("mass-matrix")
def dynamics_mass_matrix(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run_dynamics(
        dynamics_request(arm_pb2.DYNAMICS_TERM_MASS_MATRIX, joint_angles=joint_angles),
        as_json=as_json,
    )


@dynamics_app.command("inertia")
def dynamics_inertia(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
    accel: str | None = typer.Option(None, "--accel"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run_dynamics(
        dynamics_request(
            arm_pb2.DYNAMICS_TERM_INERTIA,
            joint_angles=joint_angles,
            accel=accel,
        ),
        as_json=as_json,
    )


@dynamics_app.command("inverse")
def dynamics_inverse(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
    joint_vel: str | None = typer.Option(None, "--joint-vel"),
    accel: str | None = typer.Option(None, "--accel"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run_dynamics(
        dynamics_request(
            arm_pb2.DYNAMICS_TERM_FULL_INVERSE_DYNAMICS,
            joint_angles=joint_angles,
            joint_vel=joint_vel,
            accel=accel,
        ),
        as_json=as_json,
    )


@dynamics_app.command("friction")
def dynamics_friction(
    velocity: str = typer.Option(..., "--vel"),
    fc: str | None = typer.Option(None, "--fc"),
    fv: str | None = typer.Option(None, "--fv"),
    vel_threshold: float = typer.Option(0.01, "--vel-threshold", min=0.0),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run_dynamics(
        dynamics_request(
            arm_pb2.DYNAMICS_TERM_FRICTION,
            joint_vel=velocity,
            fc=fc,
            fv=fv,
            vel_threshold=vel_threshold,
        ),
        as_json=as_json,
    )


@state_app.command("get")
def state_get(
    joints: bool = typer.Option(True, "--joints/--no-joints"),
    gripper: bool = typer.Option(True, "--gripper/--no-gripper"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    if not joints and not gripper:
        raise typer.BadParameter("joints 与 gripper 不能同时关闭")
    channel, stub = create_stub()
    data: list[dict] = []
    try:
        if joints:
            response = stub.GetJointState(arm_pb2.Empty())
            data.extend(motor_data(motor) for motor in response.joints)
        if gripper:
            response = stub.GetGripperState(arm_pb2.Empty())
            data.append(motor_data(response.state))
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
    else:
        print_motor_table(data)


@state_app.command("watch")
def state_watch(
    rate_hz: float = typer.Option(10.0, "--rate-hz", min=0.1, max=100.0),
    joints: bool = typer.Option(True, "--joints/--no-joints"),
    gripper: bool = typer.Option(True, "--gripper/--no-gripper"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    if not joints and not gripper:
        raise typer.BadParameter("joints 与 gripper 不能同时关闭")
    channel, stub = create_stub()
    try:
        for response in stub.StreamState(
            arm_pb2.StreamStateRequest(rate_hz=rate_hz, joints=joints, gripper=gripper)
        ):
            items = []
            if joints and response.HasField("joint"):
                items.extend(motor_data(motor) for motor in response.joint.joints)
            if gripper and response.HasField("gripper"):
                items.append(motor_data(response.gripper.state))
            if as_json:
                console.print_json(
                    json.dumps(
                        {
                            "age_ms": response.age_ms,
                            "estop_engaged": response.estop_engaged,
                            "motors": items,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                console.print(f"age={response.age_ms}ms estop={response.estop_engaged}")
                print_motor_table(items)
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()


@calibrate_app.command("zero")
def calibrate_zero(
    confirm: bool = typer.Option(False, "--confirm"),
    motor_ids: str | None = typer.Option(None, "--motor-ids"),
) -> None:
    if not confirm:
        console.print("[red]必须显式传入 --confirm[/red]")
        raise typer.Exit(2)
    ids = [] if motor_ids is None else [int(item.strip()) for item in motor_ids.split(",") if item.strip()]
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)
    try:
        response = stub.SetZero(
            arm_pb2.SetZeroRequest(confirm=True, motor_ids=ids),
            metadata=lease_metadata(lease),
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if not response.accepted:
        console.print(f"[red]归零被拒绝[/red]: {response.reject_reason}")
        raise typer.Exit(2)
    persistence = "已持久化" if response.persisted else "仅本次上电有效"
    console.print(f"[green]当前物理位置已重定义为零[/green]（{persistence}）")


@joint_app.command("move")
def joint_move(
    positions: str = typer.Option(..., "--pos"),
    velocities: str = typer.Option(..., "--vel"),
    max_torque: str | None = typer.Option(None, "--max-torque"),
    wait: bool = typer.Option(False, "--wait"),
    tolerance: float = typer.Option(0.1, "--tolerance", min=0.0),
    timeout: float = typer.Option(15.0, "--timeout", min=0.0),
) -> None:
    lease = load_lease()
    request = arm_pb2.JointMoveRequest(
        positions=float_list(positions, name="pos"),
        velocities=float_list(velocities, name="vel"),
        max_torque=optional_float_list(max_torque, name="max-torque"),
        wait=wait,
        tolerance=tolerance,
        timeout_s=timeout,
    )
    channel, stub = create_stub(lease.endpoint)
    try:
        with maintain_heartbeat(lease) if wait else nullcontext():
            response = stub.JointMove(
                request,
                metadata=lease_metadata(lease),
                timeout=timeout + 2.0 if wait else None,
            )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    _print_joint_response(response)


@joint_app.command("movej")
def joint_movej(
    positions: str = typer.Option(..., "--pos"),
    duration: float = typer.Option(..., "--duration", min=0.001),
    max_torque: str | None = typer.Option(None, "--max-torque"),
    wait: bool = typer.Option(False, "--wait"),
    tolerance: float = typer.Option(0.1, "--tolerance", min=0.0),
    timeout: float = typer.Option(15.0, "--timeout", min=0.0),
) -> None:
    lease = load_lease()
    request = arm_pb2.MoveJRequest(
        positions=float_list(positions, name="pos"),
        duration_s=duration,
        max_torque=optional_float_list(max_torque, name="max-torque"),
        wait=wait,
        tolerance=tolerance,
        timeout_s=timeout,
    )
    channel, stub = create_stub(lease.endpoint)
    try:
        with maintain_heartbeat(lease) if wait else nullcontext():
            response = stub.MoveJ(
                request,
                metadata=lease_metadata(lease),
                timeout=timeout + 2.0 if wait else None,
            )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    _print_joint_response(response)


@joint_app.command("jog")
def joint_jog(
    velocities: str = typer.Option(..., "--vel"),
    duration: float | None = typer.Option(None, "--duration", min=0.01),
    interactive: bool = typer.Option(False, "--interactive"),
) -> None:
    if duration is None and not interactive:
        raise typer.BadParameter("必须提供 --duration，或使用 --interactive 并按 Ctrl+C 停止")
    values = float_list(velocities, name="vel")
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)

    def commands():
        started = time.monotonic()
        while duration is None or time.monotonic() - started < duration:
            yield arm_pb2.JointJogCommand(velocities=values)
            time.sleep(0.05)

    last_feedback = None
    try:
        for feedback in stub.JointJog(commands(), metadata=lease_metadata(lease)):
            last_feedback = feedback
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if last_feedback is not None:
        hits = [index + 1 for index, hit in enumerate(last_feedback.limit_hit) if hit]
        console.print(f"[green]jog 已停止[/green] limit_hit={hits}")


@joint_app.command("mit")
def joint_mit(
    positions: str = typer.Option(..., "--pos"),
    velocities: str = typer.Option(..., "--vel"),
    torques: str = typer.Option(..., "--tqe"),
    kp: str = typer.Option(..., "--kp"),
    kd: str = typer.Option(..., "--kd"),
    stream: Path | None = typer.Option(None, "--stream", exists=True, dir_okay=False),
) -> None:
    lease = load_lease()
    initial = arm_pb2.JointMITCommand(
        positions=float_list(positions, name="pos"),
        velocities=float_list(velocities, name="vel"),
        torques=float_list(torques, name="tqe"),
        kp=float_list(kp, name="kp"),
        kd=float_list(kd, name="kd"),
    )

    def commands():
        yield initial
        if stream is None:
            time.sleep(0.1)
            return
        started = time.monotonic()
        for line_number, line in enumerate(stream.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                target_time = float(item.get("t", 0.0))
                delay = target_time - (time.monotonic() - started)
                if delay > 0:
                    time.sleep(delay)
                yield arm_pb2.JointMITCommand(
                    positions=item["pos"],
                    velocities=item["vel"],
                    torques=item.get("tqe", item.get("torques")),
                    kp=item["kp"],
                    kd=item["kd"],
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{stream}:{line_number} MIT 帧无效: {exc}") from exc

    channel, stub = create_stub(lease.endpoint)
    feedback_count = 0
    try:
        with maintain_heartbeat(lease):
            for _ in stub.JointMIT(commands(), metadata=lease_metadata(lease)):
                feedback_count += 1
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"[green]MIT 流已结束[/green] feedback={feedback_count}")


@gripper_app.command("move")
def gripper_move(
    position: float = typer.Option(..., "--pos"),
    velocity: float = typer.Option(..., "--vel"),
    max_torque: float = typer.Option(0.5, "--max-torque"),
) -> None:
    _run_gripper(
        "move",
        arm_pb2.GripperMoveRequest(position=position, velocity=velocity, max_torque=max_torque),
    )


@gripper_app.command("open")
def gripper_open(
    position: float = typer.Option(1.6, "--pos"),
    velocity: float = typer.Option(0.5, "--vel"),
    max_torque: float = typer.Option(0.5, "--max-torque"),
) -> None:
    _run_gripper(
        "open",
        arm_pb2.GripperOpenRequest(position=position, velocity=velocity, max_torque=max_torque),
    )


@gripper_app.command("close")
def gripper_close(
    position: float = typer.Option(0.0, "--pos"),
    velocity: float = typer.Option(0.5, "--vel"),
    max_torque: float = typer.Option(0.5, "--max-torque"),
) -> None:
    _run_gripper(
        "close",
        arm_pb2.GripperCloseRequest(position=position, velocity=velocity, max_torque=max_torque),
    )


@gripper_app.command("mit")
def gripper_mit(
    position: float = typer.Option(..., "--pos"),
    velocity: float = typer.Option(..., "--vel"),
    torque: float = typer.Option(..., "--tqe"),
    kp: float = typer.Option(..., "--kp", min=0.0),
    kd: float = typer.Option(..., "--kd", min=0.0),
) -> None:
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)
    try:
        response = stub.GripperMIT(
            arm_pb2.GripperMITCommand(
                position=position,
                velocity=velocity,
                torque=torque,
                kp=kp,
                kd=kd,
            ),
            metadata=lease_metadata(lease),
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if not response.accepted:
        console.print(f"[red]夹爪 MIT 被拒绝[/red]: {response.reject_reason}")
        raise typer.Exit(2)
    console.print("[green]夹爪 MIT 指令已接受[/green]")


@kinematics_app.command("fk")
def kinematics_fk(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    channel, stub = create_stub()
    try:
        response = stub.GetForwardKinematics(
            arm_pb2.JointAnglesOptional(joint_angles=optional_float_list(joint_angles, name="joint-angles")),
            timeout=10.0,
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    data = {
        "position": list(response.position),
        "rotation_matrix": list(response.rotation_matrix),
        "transform": list(response.transform),
        "used_joint_angles": list(response.used_joint_angles),
    }
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
    else:
        console.print(data)


@kinematics_app.command("jacobian")
def kinematics_jacobian(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    channel, stub = create_stub()
    try:
        response = stub.GetJacobian(
            arm_pb2.JointAnglesOptional(joint_angles=optional_float_list(joint_angles, name="joint-angles")),
            timeout=10.0,
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    matrix = [
        list(response.matrix[row * response.cols : (row + 1) * response.cols]) for row in range(response.rows)
    ]
    if as_json:
        console.print_json(json.dumps(matrix))
    else:
        console.print(matrix)


@kinematics_app.command("manipulability")
def kinematics_manipulability(
    joint_angles: str | None = typer.Option(None, "--joint-angles"),
) -> None:
    channel, stub = create_stub()
    try:
        response = stub.GetManipulability(
            arm_pb2.JointAnglesOptional(joint_angles=optional_float_list(joint_angles, name="joint-angles")),
            timeout=10.0,
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"mu={response.mu:.9g}")


@kinematics_app.command("ik")
def kinematics_ik(
    position: str = typer.Option(..., "--pos"),
    rpy: str | None = typer.Option(None, "--rpy"),
    init_q: str | None = typer.Option(None, "--init-q"),
    multi_init: bool = typer.Option(True, "--multi-init/--single-init"),
    num_attempts: int = typer.Option(8, "--num-attempts", min=1),
    timeout: float = typer.Option(0.5, "--timeout", min=0.0),
) -> None:
    channel, stub = create_stub()
    try:
        response = stub.GetInverseKinematics(
            arm_pb2.InverseKinematicsRequest(
                target=cartesian_pose(position, rpy),
                init_q=optional_float_list(init_q, name="init-q"),
                multi_init=multi_init,
                num_attempts=num_attempts,
                timeout_s=timeout,
            ),
            timeout=timeout + 5.0,
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if not response.found:
        console.print(f"[yellow]未找到逆解[/yellow] timeout={response.timeout}")
        raise typer.Exit(2)
    console.print(
        f"joint_angles={[round(value, 6) for value in response.joint_angles]} error={response.error:.6g}"
    )


@cartesian_app.command("plan-preview")
def cartesian_plan_preview(
    waypoints: str = typer.Option(..., "--waypoints"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    poses = []
    for item in waypoints.split(";"):
        values = float_list(item, name="waypoint", length=6)
        poses.append(
            arm_pb2.CartesianPose(
                position=values[:3],
                rpy=arm_pb2.RPY(roll=values[3], pitch=values[4], yaw=values[5]),
            )
        )
    channel, stub = create_stub()
    try:
        response = stub.PlanCartesianPath(
            arm_pb2.PlanCartesianPathRequest(waypoints=poses),
            timeout=15.0,
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    data = {
        "fraction": response.fraction,
        "points": [
            {
                "positions": list(point.positions),
                "velocities": list(point.velocities),
                "timestamp_s": point.timestamp_s,
            }
            for point in response.joint_trajectory
        ],
    }
    if as_json:
        console.print_json(json.dumps(data, ensure_ascii=False))
    else:
        console.print(f"fraction={response.fraction:.3f} points={len(response.joint_trajectory)}")


@cartesian_app.command("movel")
def cartesian_movel(
    position: str = typer.Option(..., "--pos"),
    rpy: str | None = typer.Option(None, "--rpy"),
    duration: float | None = typer.Option(None, "--duration", min=0.001),
    spline: bool = typer.Option(True, "--spline/--no-spline"),
    max_torque: str | None = typer.Option(None, "--max-torque"),
) -> None:
    lease = load_lease()
    request = arm_pb2.MoveLRequest(
        target=cartesian_pose(position, rpy),
        use_spline=spline,
        max_torque=optional_float_list(max_torque, name="max-torque"),
    )
    if duration is not None:
        request.duration_s = duration
    channel, stub = create_stub(lease.endpoint)
    try:
        with maintain_heartbeat(lease):
            accepted = stub.MoveL(request, metadata=lease_metadata(lease), timeout=20.0)
            console.print(f"execution_id={accepted.execution_id}")
            try:
                final = _watch_execution(stub, accepted.execution_id)
            except KeyboardInterrupt:
                stub.CancelExecution(
                    arm_pb2.CancelExecutionRequest(execution_id=accepted.execution_id),
                    metadata=lease_metadata(lease),
                )
                final = _watch_execution(stub, accepted.execution_id)
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if final.state != arm_pb2.EXEC_STATE_DONE:
        console.print(f"[yellow]moveL 终态={arm_pb2.ExecState.Name(final.state)}[/yellow]")
        raise typer.Exit(2)


@trajectory_app.command("run-waypoints")
def trajectory_run_waypoints(
    waypoints_file: Path = typer.Option(..., "--waypoints-file", exists=True, dir_okay=False),
    durations: str | None = typer.Option(None, "--durations"),
) -> None:
    try:
        payload = json.loads(waypoints_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"waypoints 文件必须是 JSON: {exc}") from exc
    if isinstance(payload, dict):
        raw_waypoints = payload.get("waypoints")
        raw_durations = payload.get("durations", [])
    else:
        raw_waypoints = payload
        raw_durations = []
    if not isinstance(raw_waypoints, list) or len(raw_waypoints) < 2:
        raise typer.BadParameter("waypoints 文件至少需要 2 个路径点")
    if durations is not None:
        try:
            raw_durations = [float(value.strip()) for value in durations.split(",") if value.strip()]
        except ValueError as exc:
            raise typer.BadParameter("durations 必须是逗号分隔数值") from exc
    if not isinstance(raw_durations, list) or len(raw_durations) != len(raw_waypoints) - 1:
        raise typer.BadParameter("durations 数量必须比 waypoints 少 1")
    request = arm_pb2.RunJointTrajectoryRequest(durations=raw_durations)
    for index, item in enumerate(raw_waypoints):
        if isinstance(item, list):
            positions_value = item
            velocities_value = []
        elif isinstance(item, dict):
            positions_value = item.get("positions", item.get("pos"))
            velocities_value = item.get("velocities", item.get("vel", []))
        else:
            raise typer.BadParameter(f"waypoints[{index}] 必须是数组或对象")
        if positions_value is None:
            raise typer.BadParameter(f"waypoints[{index}] 缺少 positions")
        request.waypoints.add(
            positions=list(positions_value),
            velocities=list(velocities_value),
        )

    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)
    try:
        with maintain_heartbeat(lease):
            accepted = stub.RunJointTrajectory(
                request,
                metadata=lease_metadata(lease),
                timeout=20.0,
            )
            console.print(f"execution_id={accepted.execution_id}")
            try:
                final = _watch_execution(stub, accepted.execution_id)
            except KeyboardInterrupt:
                stub.CancelExecution(
                    arm_pb2.CancelExecutionRequest(execution_id=accepted.execution_id),
                    metadata=lease_metadata(lease),
                )
                final = _watch_execution(stub, accepted.execution_id)
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if final.state != arm_pb2.EXEC_STATE_DONE:
        console.print(f"[yellow]trajectory 终态={arm_pb2.ExecState.Name(final.state)}[/yellow]")
        raise typer.Exit(2)


@execution_app.command("watch")
def execution_watch(execution_id: str = typer.Argument(...)) -> None:
    channel, stub = create_stub()
    try:
        final = _watch_execution(stub, execution_id)
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"终态={arm_pb2.ExecState.Name(final.state)}")


@execution_app.command("cancel")
def execution_cancel(execution_id: str = typer.Argument(...)) -> None:
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)
    try:
        response = stub.CancelExecution(
            arm_pb2.CancelExecutionRequest(execution_id=execution_id),
            metadata=lease_metadata(lease),
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    console.print(f"cancelled={response.cancelled}")


def _watch_execution(stub, execution_id: str):
    final = None
    for status in stub.StreamExecution(arm_pb2.StreamExecutionRequest(execution_id=execution_id)):
        final = status
        console.print(
            f"{arm_pb2.ExecState.Name(status.state)} fraction={status.fraction:.3f}",
            end="\r" if status.state == arm_pb2.EXEC_STATE_RUNNING else "\n",
        )
    return final


def _run_gripper(method: str, request) -> None:
    lease = load_lease()
    channel, stub = create_stub(lease.endpoint)
    try:
        response = getattr(
            stub, {"move": "GripperMove", "open": "GripperOpen", "close": "GripperClose"}[method]
        )(
            request,
            metadata=lease_metadata(lease),
        )
    except grpc.RpcError as exc:
        fail_rpc(exc)
    finally:
        channel.close()
    if not response.accepted:
        console.print(f"[red]夹爪命令被拒绝[/red]: {response.reject_reason}")
        raise typer.Exit(2)
    console.print("[green]夹爪命令已接受[/green]")


def _print_joint_response(response) -> None:
    if not response.accepted:
        console.print(f"[red]关节命令被拒绝[/red]: {response.reject_reason}")
        raise typer.Exit(2)
    console.print(
        f"[green]关节命令已接受[/green] reached={response.reached} "
        f"errors={[round(value, 6) for value in response.errors]}"
    )


if __name__ == "__main__":
    app()
