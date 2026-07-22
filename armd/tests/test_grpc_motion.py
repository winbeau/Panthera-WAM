from __future__ import annotations

import asyncio

import grpc
import pytest
import pytest_asyncio
from panthera_arm import arm_pb2, arm_pb2_grpc

from armd.backend import FrameMode, SimBackend
from armd.control import LEASE_METADATA_KEY
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer


@pytest_asyncio.fixture
async def motion_stack():
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(
        loop,
        bind="127.0.0.1:0",
        lease_timeout_s=2.0,
        watchdog_poll_s=0.02,
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{server.port}", options=(("grpc.enable_http_proxy", 0),))
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="motion-test"))
    metadata = ((LEASE_METADATA_KEY, acquired.lease_token),)
    try:
        yield loop, stub, metadata
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


@pytest.mark.asyncio
async def test_m2_state_stream_and_zero_semantics(motion_stack) -> None:
    loop, stub, metadata = motion_stack
    joint = await stub.GetJointState(arm_pb2.Empty())
    gripper = await stub.GetGripperState(arm_pb2.Empty())
    assert len(joint.joints) == 6
    assert gripper.state.motor_id == 7

    stream = stub.StreamState(arm_pb2.StreamStateRequest(rate_hz=50.0, joints=True, gripper=True))
    first = await stream.read()
    second = await stream.read()
    stream.cancel()
    assert first.HasField("joint") and first.HasField("gripper")
    assert second.age_ms < 100

    with pytest.raises(grpc.aio.AioRpcError) as missing_confirm:
        await stub.SetZero(arm_pb2.SetZeroRequest(), metadata=metadata)
    assert missing_confirm.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    selected = await stub.SetZero(
        arm_pb2.SetZeroRequest(confirm=True, motor_ids=[1, 7]),
        metadata=metadata,
    )
    assert selected.accepted and selected.persisted

    all_motors = await stub.SetZero(
        arm_pb2.SetZeroRequest(confirm=True),
        metadata=metadata,
    )
    assert all_motors.accepted and not all_motors.persisted

    await asyncio.wrap_future(loop.submit(lambda backend: backend.set_motor_connected(3, False)))
    await asyncio.sleep(0.02)
    invalid = await stub.GetJointState(arm_pb2.Empty())
    assert not invalid.joints[2].valid
    assert invalid.joints[2].position == 0.0


@pytest.mark.asyncio
async def test_joint_move_wait_and_limit_rejection(motion_stack) -> None:
    _, stub, metadata = motion_stack
    rejected = await stub.JointMove(
        arm_pb2.JointMoveRequest(
            positions=[2.5, 0.0, 0.0, 0.0, 0.0, 0.0],
            velocities=[0.5] * 6,
        ),
        metadata=metadata,
    )
    assert not rejected.accepted
    assert "joint1" in rejected.reject_reason and "上限" in rejected.reject_reason

    moved = await stub.JointMove(
        arm_pb2.JointMoveRequest(
            positions=[0.02, 0.0, 0.0, 0.0, 0.0, 0.0],
            velocities=[0.5] * 6,
            wait=True,
            tolerance=0.001,
            timeout_s=1.0,
        ),
        metadata=metadata,
        timeout=2.0,
    )
    assert moved.accepted and moved.reached
    assert moved.errors[0] <= 0.001


@pytest.mark.asyncio
async def test_movej_wait_and_gripper_commands(motion_stack) -> None:
    loop, stub, metadata = motion_stack
    moved = await stub.MoveJ(
        arm_pb2.MoveJRequest(
            positions=[0.02, 0.0, 0.0, 0.0, 0.0, 0.0],
            duration_s=0.1,
            wait=True,
            tolerance=0.001,
            timeout_s=1.0,
        ),
        metadata=metadata,
        timeout=2.0,
    )
    assert moved.accepted and moved.reached

    opened = await stub.GripperOpen(arm_pb2.GripperOpenRequest(position=0.1), metadata=metadata)
    assert opened.accepted
    frame = None
    for _ in range(20):
        await asyncio.sleep(0.01)
        frame = await asyncio.wrap_future(loop.submit(lambda backend: backend._last_frame))
        if frame is not None and frame.mode is FrameMode.POS_VEL_TQE_KP_KD and frame.gripper_kp > 0.0:
            break
    assert frame is not None
    assert frame.mode is FrameMode.POS_VEL_TQE_KP_KD
    assert frame.arm_kp == pytest.approx([0.0] * 6)
    assert frame.arm_kd == pytest.approx([0.0] * 6)
    assert frame.gripper_torque == 0.0
    assert frame.gripper_velocity > 0.0
    assert frame.gripper_kp <= 5.0
    assert frame.gripper_kd <= 0.5

    await asyncio.sleep(0.25)
    state = await stub.GetGripperState(arm_pb2.Empty())
    assert state.state.position == pytest.approx(0.1, abs=0.01)
    assert not loop.has_active_motion
    settled_frame = await asyncio.wrap_future(loop.submit(lambda backend: backend._last_frame))
    assert settled_frame is not None
    assert settled_frame.mode is FrameMode.POS_VEL_TQE_KP_KD
    assert settled_frame.arm_kp == pytest.approx([0.0] * 6)
    assert settled_frame.arm_kd == pytest.approx([0.0] * 6)
    assert settled_frame.gripper_kp == 0.0

    rejected = await stub.GripperMove(
        arm_pb2.GripperMoveRequest(position=2.1, velocity=0.5),
        metadata=metadata,
    )
    assert not rejected.accepted
    assert "上限" in rejected.reject_reason

    rejected_open = await stub.GripperOpen(
        arm_pb2.GripperOpenRequest(position=2.1),
        metadata=metadata,
    )
    assert not rejected_open.accepted
    assert "目标" in rejected_open.reject_reason and "上限" in rejected_open.reject_reason

    rejected_close = await stub.GripperClose(
        arm_pb2.GripperCloseRequest(position=-0.1),
        metadata=metadata,
    )
    assert not rejected_close.accepted
    assert "目标" in rejected_close.reject_reason and "下限" in rejected_close.reject_reason


@pytest.mark.asyncio
async def test_movej_preserves_official_signed_velocity(motion_stack) -> None:
    loop, stub, metadata = motion_stack
    signed = await stub.MoveJ(
        arm_pb2.MoveJRequest(
            positions=[-0.2, 0.0, 0.0, 0.0, 0.0, 0.0],
            duration_s=1.0,
            wait=False,
        ),
        metadata=metadata,
        timeout=2.0,
    )
    assert signed.accepted

    signed_frame = None
    for _ in range(20):
        signed_frame = await asyncio.wrap_future(loop.submit(lambda backend: backend._last_frame))
        if signed_frame is not None and signed_frame.arm_velocity[0] < 0.0:
            break
        await asyncio.sleep(0.005)
    assert signed_frame is not None
    assert signed_frame.arm_velocity[0] == pytest.approx(-0.2)


@pytest.mark.asyncio
async def test_estop_recovery_then_gripper_keeps_joint_firmware_kd_zero(motion_stack) -> None:
    loop, stub, metadata = motion_stack
    await stub.EStop(arm_pb2.EStopRequest(reason="gripper-regression"))
    cleared = await stub.ClearEStop(
        arm_pb2.ClearEStopRequest(confirm=True),
        metadata=metadata,
    )
    assert not cleared.engaged

    opened = await stub.GripperOpen(
        arm_pb2.GripperOpenRequest(position=0.05, velocity=0.2),
        metadata=metadata,
    )
    assert opened.accepted

    active_frame = None
    for _ in range(30):
        await asyncio.sleep(0.01)
        active_frame = await asyncio.wrap_future(loop.submit(lambda backend: backend._last_frame))
        if active_frame is not None and active_frame.gripper_kp > 0.0:
            break
    assert active_frame is not None
    assert active_frame.arm_kp == pytest.approx([0.0] * 6)
    assert active_frame.arm_kd == pytest.approx([0.0] * 6)

    for _ in range(200):
        if not loop.has_active_motion:
            break
        await asyncio.sleep(0.01)
    assert not loop.has_active_motion
    settled_frame = await asyncio.wrap_future(loop.submit(lambda backend: backend._last_frame))
    assert settled_frame is not None
    assert settled_frame.arm_kp == pytest.approx([0.0] * 6)
    assert settled_frame.arm_kd == pytest.approx([0.0] * 6)


@pytest.mark.asyncio
async def test_joint_jog_stops_after_freshness_window_and_stream_close(motion_stack) -> None:
    _, stub, metadata = motion_stack
    call = stub.JointJog(metadata=metadata)
    await call.write(arm_pb2.JointJogCommand(velocities=[0.2, 0.0, 0.0, 0.0, 0.0, 0.0]))
    feedback = await call.read()
    assert len(feedback.joint_state.joints) == 6
    await asyncio.sleep(0.12)
    moving = await stub.GetJointState(arm_pb2.Empty())
    assert moving.joints[0].position > 0.0

    await asyncio.sleep(0.3)
    first = await stub.GetJointState(arm_pb2.Empty())
    await asyncio.sleep(0.1)
    second = await stub.GetJointState(arm_pb2.Empty())
    assert second.joints[0].position == pytest.approx(first.joints[0].position, abs=1e-4)

    await call.done_writing()
    while await call.read() is not grpc.aio.EOF:
        pass


@pytest.mark.asyncio
async def test_unary_polling_heartbeat_and_jog_fallback(motion_stack) -> None:
    loop, stub, metadata = motion_stack
    robot = await stub.GetRobotState(arm_pb2.Empty())
    assert len(robot.joint.joints) == 6
    assert robot.gripper.state.motor_id == 7

    for _ in range(12):
        heartbeat = await stub.HeartbeatOnce(arm_pb2.HeartbeatRequest(), metadata=metadata)
        feedback = await stub.JointJogStep(
            arm_pb2.JointJogCommand(velocities=[0.1, 0.0, 0.0, 0.0, 0.0, 0.0]),
            metadata=metadata,
        )
        assert heartbeat.ok
        assert len(feedback.joint_state.joints) == 6
        await asyncio.sleep(0.04)

    moving = await stub.GetRobotState(arm_pb2.Empty())
    assert moving.joint.joints[0].position > 0.0
    await stub.StopJointJog(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.05)
    assert not loop.has_active_motion
