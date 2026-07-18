"""panthera-cli M1 命令。"""

from __future__ import annotations

import json
import time
from contextlib import nullcontext

import grpc
import typer
from panthera_arm import arm_pb2
from rich.console import Console
from rich.table import Table

from .client import (
    SavedLease,
    clear_lease,
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
app.add_typer(control_app, name="control")
app.add_typer(estop_app, name="estop")
app.add_typer(safety_app, name="safety")
app.add_typer(daemon_app, name="daemon")
app.add_typer(state_app, name="state")
app.add_typer(calibrate_app, name="calibrate")
app.add_typer(joint_app, name="joint")
app.add_typer(gripper_app, name="gripper")
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


def fail_rpc(exc: grpc.RpcError) -> None:
    detail = exc.details() if hasattr(exc, "details") else str(exc)
    code = exc.code().name if hasattr(exc, "code") else "RPC_ERROR"
    console.print(f"[red]{code}[/red]: {detail}")
    raise typer.Exit(1)


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
