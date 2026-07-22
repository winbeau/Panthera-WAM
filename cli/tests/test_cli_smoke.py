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
from panthera_cli.client import camera_endpoint


EXPECTED_COMMANDS = {
    "calibrate zero",
    "camera snapshot",
    "camera status",
    "camera stream",
    "cartesian movel",
    "cartesian jog",
    "cartesian plan-preview",
    "control acquire",
    "control heartbeat",
    "control release",
    "control status",
    "daemon status",
    "daemon version",
    "dataset cancel",
    "dataset export-lerobot",
    "dataset mapping",
    "dataset status",
    "dynamics coriolis",
    "dynamics friction",
    "dynamics gravity",
    "dynamics inertia",
    "dynamics inverse",
    "dynamics mass-matrix",
    "estop reset",
    "estop trigger",
    "execution cancel",
    "execution watch",
    "gripper close",
    "gripper move",
    "gripper open",
    "gripper mit",
    "joint jog",
    "joint mit",
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
    "trajectory run-waypoints",
    "teach list",
    "teach play",
    "teach record start",
    "teach record stop",
    "teach start",
    "teach stop",
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


def test_command_inventory_is_explicit() -> None:
    root = get_command(app)
    assert hasattr(root, "commands")
    assert command_paths(root) == EXPECTED_COMMANDS


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_camera_endpoint_uses_dedicated_port(monkeypatch) -> None:
    monkeypatch.delenv("PANTHERA_CAMERA_ENDPOINT", raising=False)
    monkeypatch.setenv("PANTHERA_ENDPOINT", "192.168.1.20:50051")
    assert camera_endpoint() == "192.168.1.20:50052"
    monkeypatch.setenv("PANTHERA_ENDPOINT", "[::1]:50051")
    assert camera_endpoint() == "[::1]:50052"


def test_cli_control_estop_and_status(tmp_path, monkeypatch) -> None:
    port = free_port()
    endpoint = f"127.0.0.1:{port}"
    env = os.environ.copy()
    env["PANTHERA_ENDPOINT"] = endpoint
    env["PANTHERA_CAMERA_ENDPOINT"] = endpoint
    env["PANTHERA_STATE_DIR"] = str(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "armd",
            "--sim",
            "--camera-mode",
            "sim",
            "--camera-width",
            "8",
            "--camera-height",
            "6",
            "--camera-fps",
            "30",
            "--bind",
            endpoint,
            "--lease-timeout",
            "30",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    channel = grpc.insecure_channel(endpoint, options=(("grpc.enable_http_proxy", 0),))
    try:
        grpc.channel_ready_future(channel).result(timeout=15)
        monkeypatch.setenv("PANTHERA_ENDPOINT", endpoint)
        monkeypatch.setenv("PANTHERA_CAMERA_ENDPOINT", endpoint)
        monkeypatch.setenv("PANTHERA_STATE_DIR", str(tmp_path))
        runner = CliRunner()

        camera = runner.invoke(app, ["camera", "status", "--json"])
        assert camera.exit_code == 0, camera.output
        assert '"available": true' in camera.output

        mapping = runner.invoke(app, ["dataset", "mapping", "--json"])
        assert mapping.exit_code == 0, mapping.output
        assert '"format_version": "LeRobotDataset v3.0"' in mapping.output
        assert '"target": "observation.state"' in mapping.output

        snapshot_path = tmp_path / "depth.pgm"
        snapshot = runner.invoke(
            app,
            ["camera", "snapshot", "--stream", "depth", "--out", str(snapshot_path)],
        )
        assert snapshot.exit_code == 0, snapshot.output
        assert snapshot_path.read_bytes().startswith(b"P5\n8 6\n65535\n")
        assert snapshot_path.with_suffix(".pgm.json").is_file()

        camera_stream = runner.invoke(
            app,
            ["camera", "stream", "--stream", "color", "--frames", "2"],
        )
        assert camera_stream.exit_code == 0, camera_stream.output
        assert "received=2" in camera_stream.output

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
