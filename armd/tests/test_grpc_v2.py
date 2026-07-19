from __future__ import annotations

import asyncio
import json
from pathlib import Path

import grpc
import numpy as np
import pytest
import pytest_asyncio
from panthera_arm import arm_pb2, arm_pb2_grpc

from armd.backend import SimBackend
from armd.control import LEASE_METADATA_KEY
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer


@pytest_asyncio.fixture
async def v2_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("PANTHERA_TEACH_DIR", str(tmp_path / "teach"))
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(loop, bind="127.0.0.1:0", lease_timeout_s=10.0)
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="v2-test"))
    metadata = ((LEASE_METADATA_KEY, acquired.lease_token),)
    try:
        yield loop, stub, metadata, server
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


@pytest.mark.asyncio
async def test_m5_dynamics_terms_and_friction_defaults(v2_stack) -> None:
    _, stub, _, _ = v2_stack
    q = [0.1, 0.3, 0.4, 0.0, -0.1, 0.2]
    v = [0.1, -0.1, 0.0, 0.02, -0.02, 0.001]
    a = [0.2] * 6

    gravity = await stub.GetDynamicsTerm(
        arm_pb2.DynamicsQueryRequest(term=arm_pb2.DYNAMICS_TERM_GRAVITY, q=q)
    )
    coriolis = await stub.GetDynamicsTerm(
        arm_pb2.DynamicsQueryRequest(term=arm_pb2.DYNAMICS_TERM_CORIOLIS, q=q, v=v)
    )
    mass = await stub.GetDynamicsTerm(
        arm_pb2.DynamicsQueryRequest(term=arm_pb2.DYNAMICS_TERM_MASS_MATRIX, q=q)
    )
    inertia = await stub.GetDynamicsTerm(
        arm_pb2.DynamicsQueryRequest(term=arm_pb2.DYNAMICS_TERM_INERTIA, q=q, a=a)
    )
    inverse = await stub.GetDynamicsTerm(
        arm_pb2.DynamicsQueryRequest(
            term=arm_pb2.DYNAMICS_TERM_FULL_INVERSE_DYNAMICS,
            q=q,
            v=v,
            a=a,
        )
    )
    friction = await stub.GetDynamicsTerm(
        arm_pb2.DynamicsQueryRequest(term=arm_pb2.DYNAMICS_TERM_FRICTION, v=v)
    )

    assert len(gravity.gravity) == 6
    assert len(coriolis.coriolis_matrix) == 36
    assert len(coriolis.coriolis_vector) == 6
    assert len(mass.mass_matrix) == 36
    assert len(inertia.inertia_terms) == 6
    assert len(inverse.inverse_dynamics) == 6
    expected = np.array([0.20, -0.15, 0.0, 0.15, -0.04, 0.00002]) + np.array(
        [0.006, -0.006, 0.0, 0.0006, -0.0004, 0.0]
    )
    expected[2] = 0.0
    expected[5] = 0.02 * 0.001
    assert np.asarray(friction.friction_compensation) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_m5_joint_and_gripper_mit(v2_stack) -> None:
    loop, stub, metadata, _ = v2_stack
    call = stub.JointMIT(metadata=metadata)
    await call.write(
        arm_pb2.JointMITCommand(
            positions=[0.05, 0.0, 0.0, 0.0, 0.0, 0.0],
            velocities=[0.0] * 6,
            torques=[0.0] * 6,
            kp=[4.0] * 6,
            kd=[0.5] * 6,
        )
    )
    feedback = await call.read()
    assert len(feedback.joint_state.joints) == 6
    await asyncio.sleep(0.1)
    moving = await stub.GetJointState(arm_pb2.Empty())
    assert moving.joints[0].position > 0.0
    await call.done_writing()
    while await call.read() is not grpc.aio.EOF:
        pass
    await asyncio.sleep(0.05)
    assert not loop.has_active_motion

    gripper = await stub.GripperMIT(
        arm_pb2.GripperMITCommand(position=0.1, velocity=0.0, torque=0.0, kp=5.0, kd=0.5),
        metadata=metadata,
    )
    assert gripper.accepted


@pytest.mark.asyncio
async def test_m5_cartesian_jog(v2_stack) -> None:
    loop, stub, metadata, _ = v2_stack
    call = stub.CartesianJog(metadata=metadata)
    await call.write(
        arm_pb2.CartesianJogCommand(
            linear_velocity=[0.005, 0.0, 0.0],
            angular_velocity=[0.0, 0.0, 0.0],
            damping=0.02,
        )
    )
    feedback = await call.read()
    assert len(feedback.joint_state.joints) == 6
    assert feedback.manipulability >= 0.0
    await asyncio.sleep(0.1)
    await call.done_writing()
    while await call.read() is not grpc.aio.EOF:
        pass
    await asyncio.sleep(0.05)
    assert not loop.has_active_motion


@pytest.mark.asyncio
async def test_m6_joint_trajectory_zero_velocity_and_cancel(v2_stack) -> None:
    _, stub, metadata, _ = v2_stack
    completed = await stub.RunJointTrajectory(
        arm_pb2.RunJointTrajectoryRequest(
            waypoints=[
                arm_pb2.WaypointSpec(positions=[0.0] * 6),
                arm_pb2.WaypointSpec(positions=[0.02, 0.0, 0.0, 0.0, 0.0, 0.0]),
            ],
            durations=[0.3],
        ),
        metadata=metadata,
        timeout=5.0,
    )
    final = None
    fractions = []
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=completed.execution_id)
    ):
        fractions.append(status.fraction)
        final = status
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE
    assert fractions == sorted(fractions)

    current = await stub.GetJointState(arm_pb2.Empty())
    start = [motor.position for motor in current.joints]
    target = start.copy()
    target[0] += 0.08
    running = await stub.RunJointTrajectory(
        arm_pb2.RunJointTrajectoryRequest(
            waypoints=[
                arm_pb2.WaypointSpec(positions=start, velocities=[0.0] * 6),
                arm_pb2.WaypointSpec(positions=target, velocities=[0.0] * 6),
            ],
            durations=[1.0],
        ),
        metadata=metadata,
        timeout=5.0,
    )
    await asyncio.sleep(0.1)
    cancelled = await stub.CancelExecution(
        arm_pb2.CancelExecutionRequest(execution_id=running.execution_id),
        metadata=metadata,
    )
    assert cancelled.cancelled
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=running.execution_id)
    ):
        final = status
    assert final is not None and final.state == arm_pb2.EXEC_STATE_CANCELLED


@pytest.mark.asyncio
async def test_m6_joint_trajectory_with_middle_velocity(v2_stack) -> None:
    _, stub, metadata, _ = v2_stack
    accepted = await stub.RunJointTrajectory(
        arm_pb2.RunJointTrajectoryRequest(
            waypoints=[
                arm_pb2.WaypointSpec(positions=[0.0] * 6, velocities=[0.0] * 6),
                arm_pb2.WaypointSpec(
                    positions=[0.01, 0.0, 0.0, 0.0, 0.0, 0.0],
                    velocities=[0.02, 0.0, 0.0, 0.0, 0.0, 0.0],
                ),
                arm_pb2.WaypointSpec(positions=[0.02, 0.0, 0.0, 0.0, 0.0, 0.0], velocities=[0.0] * 6),
            ],
            durations=[0.4, 0.4],
        ),
        metadata=metadata,
        timeout=5.0,
    )
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE


@pytest.mark.asyncio
async def test_m7_teach_record_stop_and_list(v2_stack) -> None:
    loop, stub, metadata, _ = v2_stack
    started = await stub.TeachStart(arm_pb2.TeachStartRequest(), metadata=metadata)
    assert started.accepted
    await asyncio.sleep(0.03)
    assert loop.has_active_motion

    recording = await stub.TeachRecordStart(
        arm_pb2.TeachRecordStartRequest(path="session.jsonl", flush_interval=0.01),
        metadata=metadata,
    )
    assert recording.accepted
    await asyncio.sleep(0.08)
    saved = await stub.TeachRecordStop(arm_pb2.Empty(), metadata=metadata)
    assert saved.accepted and saved.frame_count >= 5
    lines = [json.loads(line) for line in Path(saved.saved_path).read_text().splitlines()]
    assert len(lines) == saved.frame_count
    assert set(lines[0]) == {"t", "pos", "vel", "gripper_pos", "gripper_vel"}
    assert len(lines[0]["pos"]) == 6 and len(lines[0]["vel"]) == 6

    listed = await stub.TeachList(arm_pb2.Empty())
    assert len(listed.files) == 1
    assert listed.files[0].path == saved.saved_path
    assert listed.files[0].frame_count == saved.frame_count

    stopped = await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    assert stopped.accepted
    await asyncio.sleep(0.03)
    assert not loop.has_active_motion


@pytest.mark.asyncio
async def test_m7_teach_stop_automatically_saves_active_recording(v2_stack) -> None:
    _, stub, metadata, _ = v2_stack
    started = await stub.TeachStart(arm_pb2.TeachStartRequest(), metadata=metadata)
    assert started.accepted
    recording = await stub.TeachRecordStart(
        arm_pb2.TeachRecordStartRequest(path="auto-stop.jsonl", flush_interval=0.01),
        metadata=metadata,
    )
    assert recording.accepted
    await asyncio.sleep(0.06)

    stopped = await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    assert stopped.accepted
    listed = await stub.TeachList(arm_pb2.Empty())
    saved = next(item for item in listed.files if item.path.endswith("auto-stop.jsonl"))
    assert saved.frame_count >= 5


@pytest.mark.asyncio
async def test_m7_teach_play_posvel_and_cancel(v2_stack) -> None:
    _, stub, metadata, server = v2_stack
    root = server.arm_service._teach_store.root
    complete_path = root / "complete.jsonl"
    complete_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "t": 0.0,
                        "pos": [0.0] * 6,
                        "vel": [0.0] * 6,
                        "gripper_pos": 0.0,
                        "gripper_vel": 0.0,
                    }
                ),
                json.dumps(
                    {
                        "t": 0.4,
                        "pos": [0.02, 0.0, 0.0, 0.0, 0.0, 0.0],
                        "vel": [0.05, 0.0, 0.0, 0.0, 0.0, 0.0],
                        "gripper_pos": 0.02,
                        "gripper_vel": 0.05,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    accepted = await stub.TeachPlay(
        arm_pb2.TeachPlayRequest(
            path=str(complete_path),
            mode=arm_pb2.PLAYBACK_MODE_POSVEL,
            playback_dt=0.01,
            smooth_window=1,
        ),
        metadata=metadata,
    )
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE

    current = await stub.GetJointState(arm_pb2.Empty())
    start = [joint.position for joint in current.joints]
    mit_path = root / "mit.jsonl"
    mit_path.write_text(
        json.dumps({"t": 0.0, "pos": start, "vel": [0.0] * 6})
        + "\n"
        + json.dumps({"t": 0.05, "pos": start, "vel": [0.0] * 6})
        + "\n",
        encoding="utf-8",
    )
    mit = await stub.TeachPlay(
        arm_pb2.TeachPlayRequest(
            path=str(mit_path),
            mode=arm_pb2.PLAYBACK_MODE_MIT,
            kp=[0.0] * 6,
            kd=[0.0] * 6,
            playback_dt=0.01,
            smooth_window=1,
        ),
        metadata=metadata,
    )
    async for status in stub.StreamExecution(arm_pb2.StreamExecutionRequest(execution_id=mit.execution_id)):
        final = status
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE

    cancel_path = root / "cancel.jsonl"
    target = start.copy()
    target[0] += 0.08
    cancel_path.write_text(
        json.dumps({"t": 0.0, "pos": start, "vel": [0.0] * 6})
        + "\n"
        + json.dumps({"t": 2.0, "pos": target, "vel": [0.04, 0.0, 0.0, 0.0, 0.0, 0.0]})
        + "\n",
        encoding="utf-8",
    )
    running = await stub.TeachPlay(
        arm_pb2.TeachPlayRequest(
            path=str(cancel_path),
            mode=arm_pb2.PLAYBACK_MODE_POSVEL,
            playback_dt=0.01,
            smooth_window=1,
        ),
        metadata=metadata,
    )
    await asyncio.sleep(0.1)
    cancelled = await stub.CancelExecution(
        arm_pb2.CancelExecutionRequest(execution_id=running.execution_id),
        metadata=metadata,
    )
    assert cancelled.cancelled
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=running.execution_id)
    ):
        final = status
    assert final is not None and final.state == arm_pb2.EXEC_STATE_CANCELLED
