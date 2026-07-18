from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import grpc
from typer.main import get_command
from typer.testing import CliRunner

from panthera_cli.__main__ import app


EXPECTED_V1_COMMANDS = {
    "calibrate zero",
    "cartesian movel",
    "cartesian plan-preview",
    "control acquire",
    "control heartbeat",
    "control release",
    "control status",
    "daemon status",
    "daemon version",
    "estop reset",
    "estop trigger",
    "execution cancel",
    "execution watch",
    "gripper close",
    "gripper move",
    "gripper open",
    "joint jog",
    "joint move",
    "joint movej",
    "kinematics fk",
    "kinematics ik",
    "kinematics jacobian",
    "kinematics manipulability",
    "safety check-reached",
    "safety limits show",
    "state get",
    "state watch",
}


def command_paths(group, prefix: tuple[str, ...] = ()) -> set[str]:
    paths: set[str] = set()
    for name, command in group.commands.items():
        path = (*prefix, name)
        if hasattr(command, "commands"):
            paths.update(command_paths(command, path))
        else:
            paths.add(" ".join(path))
    return paths


def test_v1_command_inventory_is_explicit() -> None:
    root = get_command(app)
    assert hasattr(root, "commands")
    assert command_paths(root) == EXPECTED_V1_COMMANDS


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_cli_control_estop_and_status(tmp_path, monkeypatch) -> None:
    port = free_port()
    endpoint = f"127.0.0.1:{port}"
    env = os.environ.copy()
    env["PANTHERA_ENDPOINT"] = endpoint
    env["PANTHERA_STATE_DIR"] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "armd", "--sim", "--bind", endpoint, "--lease-timeout", "5"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    channel = grpc.insecure_channel(endpoint, options=(("grpc.enable_http_proxy", 0),))
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        monkeypatch.setenv("PANTHERA_ENDPOINT", endpoint)
        monkeypatch.setenv("PANTHERA_STATE_DIR", str(tmp_path))
        runner = CliRunner()

        acquired = runner.invoke(app, ["control", "acquire", "--client-id", "cli-test"])
        assert acquired.exit_code == 0, acquired.output

        state = runner.invoke(app, ["state", "get", "--json"])
        assert state.exit_code == 0, state.output
        assert '"motor_id": 7' in state.output

        moved = runner.invoke(
            app,
            [
                "joint",
                "move",
                "--pos",
                "0.02,0,0,0,0,0",
                "--vel",
                "0.5,0.5,0.5,0.5,0.5,0.5",
                "--wait",
                "--tolerance",
                "0.001",
                "--timeout",
                "1",
            ],
        )
        assert moved.exit_code == 0, moved.output
        assert "reached=True" in moved.output

        movej = runner.invoke(
            app,
            [
                "joint",
                "movej",
                "--pos",
                "0.03,0,0,0,0,0",
                "--duration",
                "0.1",
                "--wait",
                "--tolerance",
                "0.001",
                "--timeout",
                "1",
            ],
        )
        assert movej.exit_code == 0, movej.output

        jog = runner.invoke(
            app,
            ["joint", "jog", "--vel", "-0.1,0,0,0,0,0", "--duration", "0.1"],
        )
        assert jog.exit_code == 0, jog.output

        gripper = runner.invoke(
            app,
            ["gripper", "open", "--pos", "0.1", "--vel", "0.5"],
        )
        assert gripper.exit_code == 0, gripper.output
        time.sleep(0.25)

        zero = runner.invoke(app, ["calibrate", "zero", "--confirm", "--motor-ids", "1,7"])
        assert zero.exit_code == 0, zero.output
        assert "已持久化" in zero.output

        zero_all = runner.invoke(app, ["calibrate", "zero", "--confirm"])
        assert zero_all.exit_code == 0, zero_all.output
        assert "仅本次上电有效" in zero_all.output

        fk = runner.invoke(app, ["kinematics", "fk", "--json"])
        assert fk.exit_code == 0, fk.output
        fk_data = json.loads(fk.output)
        target = fk_data["position"]
        target[2] += 0.004
        movel = runner.invoke(
            app,
            [
                "cartesian",
                "movel",
                "--pos",
                ",".join(str(value) for value in target),
                "--duration",
                "0.3",
            ],
        )
        assert movel.exit_code == 0, movel.output
        assert "EXEC_STATE_DONE" in movel.output

        status = runner.invoke(app, ["control", "status", "--json"])
        assert status.exit_code == 0, status.output
        assert '"held": true' in status.output

        estop = runner.invoke(app, ["estop", "trigger", "--reason", "test"])
        assert estop.exit_code == 0, estop.output
        reset = runner.invoke(app, ["estop", "reset", "--confirm"])
        assert reset.exit_code == 0, reset.output

        released = runner.invoke(app, ["control", "release"])
        assert released.exit_code == 0, released.output
    finally:
        channel.close()
        process.terminate()
        process.wait(timeout=5)
        time.sleep(0.05)
