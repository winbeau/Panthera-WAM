from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import grpc
from typer.testing import CliRunner

from panthera_cli.__main__ import app


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
    channel = grpc.insecure_channel(endpoint)
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
