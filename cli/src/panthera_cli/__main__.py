"""panthera-cli M1 命令。"""

from __future__ import annotations

import json
import time

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
    save_lease,
)

app = typer.Typer(no_args_is_help=True, help="Panthera-HT armd 命令行客户端")
control_app = typer.Typer(no_args_is_help=True)
estop_app = typer.Typer(no_args_is_help=True)
safety_app = typer.Typer(no_args_is_help=True)
limits_app = typer.Typer(no_args_is_help=True)
daemon_app = typer.Typer(no_args_is_help=True)
app.add_typer(control_app, name="control")
app.add_typer(estop_app, name="estop")
app.add_typer(safety_app, name="safety")
app.add_typer(daemon_app, name="daemon")
safety_app.add_typer(limits_app, name="limits")
console = Console()


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


if __name__ == "__main__":
    app()
