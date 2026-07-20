from __future__ import annotations

import asyncio

import grpc
import numpy as np
import pytest
import pytest_asyncio
from panthera_arm import arm_pb2, arm_pb2_grpc

from armd.backend import FrameMode, JointFrame, SMOOTH_IDLE_TORQUE_LIMIT, SimBackend
from armd.control import LEASE_METADATA_KEY
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer


@pytest_asyncio.fixture
async def grpc_stack():
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(
        loop,
        bind="127.0.0.1:0",
        lease_timeout_s=0.08,
        watchdog_poll_s=0.01,
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{server.port}", options=(("grpc.enable_http_proxy", 0),))
    await channel.channel_ready()
    try:
        yield loop, server, arm_pb2_grpc.ArmServiceStub(channel)
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


def lease_metadata(token: str):
    return ((LEASE_METADATA_KEY, token),)


def velocity_frame(value: float) -> JointFrame:
    return JointFrame(
        mode=FrameMode.VELOCITY,
        arm_position=np.zeros(6),
        arm_velocity=np.array([value, 0.0, 0.0, 0.0, 0.0, 0.0]),
        gripper_position=0.0,
        gripper_velocity=0.0,
    )


@pytest.mark.asyncio
async def test_two_clients_cannot_hold_control_simultaneously(grpc_stack) -> None:
    _, _, stub = grpc_stack
    first = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="client-a"))
    second = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="client-b"))

    assert first.granted
    assert first.lease_token
    assert not second.granted
    assert second.holder_client_id == "client-a"


@pytest.mark.asyncio
async def test_force_acquire_invalidates_previous_token(grpc_stack) -> None:
    _, _, stub = grpc_stack
    first = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="client-a"))
    second = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="client-b", force=True))

    assert second.granted
    with pytest.raises(grpc.aio.AioRpcError) as old_token:
        await stub.JointMove(arm_pb2.JointMoveRequest(), metadata=lease_metadata(first.lease_token))
    assert old_token.value.code() is grpc.StatusCode.PERMISSION_DENIED

    with pytest.raises(grpc.aio.AioRpcError) as new_token:
        await stub.JointMove(arm_pb2.JointMoveRequest(), metadata=lease_metadata(second.lease_token))
    assert new_token.value.code() is grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_mutating_rpc_requires_valid_metadata_lease(grpc_stack) -> None:
    _, _, stub = grpc_stack
    request = arm_pb2.JointMoveRequest(positions=[0.0] * 6, velocities=[0.1] * 6)

    with pytest.raises(grpc.aio.AioRpcError) as missing:
        await stub.JointMove(request)
    assert missing.value.code() is grpc.StatusCode.PERMISSION_DENIED

    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="holder"))
    accepted = await stub.JointMove(request, metadata=lease_metadata(acquired.lease_token))
    assert accepted.accepted


@pytest.mark.asyncio
async def test_estop_is_lock_free_and_blocks_motion_until_confirmed_clear(grpc_stack) -> None:
    loop, _, stub = grpc_stack
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="holder"))
    metadata = lease_metadata(acquired.lease_token)

    stopped = await stub.EStop(arm_pb2.EStopRequest(reason="test"))
    assert stopped.engaged
    assert loop.estop_applied

    with pytest.raises(grpc.aio.AioRpcError) as blocked:
        await stub.JointMove(arm_pb2.JointMoveRequest(), metadata=metadata)
    assert blocked.value.code() is grpc.StatusCode.FAILED_PRECONDITION

    with pytest.raises(grpc.aio.AioRpcError) as unconfirmed:
        await stub.ClearEStop(arm_pb2.ClearEStopRequest(confirm=False), metadata=metadata)
    assert unconfirmed.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    cleared = await stub.ClearEStop(arm_pb2.ClearEStopRequest(confirm=True), metadata=metadata)
    assert not cleared.engaged
    assert not loop.estop_engaged
    assert loop.estop_recovery_applied


@pytest.mark.asyncio
async def test_release_control_enters_smooth_idle_damping(grpc_stack) -> None:
    loop, _, stub = grpc_stack
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="holder"))
    metadata = lease_metadata(acquired.lease_token)
    moved = await stub.MoveJ(
        arm_pb2.MoveJRequest(
            positions=[0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
            duration_s=1.0,
            wait=False,
        ),
        metadata=metadata,
    )
    assert moved.accepted
    assert loop.has_active_motion

    await stub.ReleaseControl(arm_pb2.Empty(), metadata=metadata)
    assert not loop.has_active_motion
    await asyncio.sleep(0.01)
    frame = await asyncio.wrap_future(loop.submit(lambda backend: backend._last_frame))

    assert frame is not None
    assert frame.mode is FrameMode.POS_VEL_TQE_KP_KD
    assert np.all(np.isfinite(frame.arm_torque))
    assert np.all(np.abs(frame.arm_torque) <= SMOOTH_IDLE_TORQUE_LIMIT)
    assert frame.arm_kp == pytest.approx([0.0] * 6)
    assert frame.arm_kd == pytest.approx([0.0] * 6)


@pytest.mark.asyncio
async def test_watchdog_releases_lease_and_zeroes_velocity(grpc_stack) -> None:
    loop, _, stub = grpc_stack
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="holder"))
    await asyncio.wrap_future(loop.submit(lambda backend: backend.write_frame(velocity_frame(0.5))))
    await asyncio.sleep(0.04)

    await asyncio.sleep(0.1)
    status = await stub.GetControlStatus(arm_pb2.Empty())
    first = loop.latest_state().motors[0].position
    await asyncio.sleep(0.05)
    second = loop.latest_state().motors[0].position

    assert acquired.granted
    assert not status.held
    assert status.watchdog_ok
    assert second == pytest.approx(first, abs=1e-6)
    frame = await asyncio.wrap_future(loop.submit(lambda backend: backend._last_frame))
    assert frame is not None
    assert np.all(np.abs(frame.arm_torque) <= SMOOTH_IDLE_TORQUE_LIMIT)
    assert frame.arm_kd == pytest.approx([0.0] * 6)


@pytest.mark.asyncio
async def test_heartbeat_stream_keeps_lease_alive(grpc_stack) -> None:
    _, _, stub = grpc_stack
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="holder"))

    async def requests():
        for _ in range(6):
            yield arm_pb2.HeartbeatRequest()
            await asyncio.sleep(0.02)

    responses = []
    async for response in stub.Heartbeat(requests(), metadata=lease_metadata(acquired.lease_token)):
        responses.append(response)
    status = await stub.GetControlStatus(arm_pb2.Empty())

    assert len(responses) == 6
    assert all(response.ok for response in responses)
    assert status.held
    assert status.watchdog_ok


@pytest.mark.asyncio
async def test_soft_limits_and_daemon_status_are_read_only(grpc_stack) -> None:
    _, _, stub = grpc_stack
    limits = await stub.GetSoftLimits(arm_pb2.Empty())
    status = await stub.GetDaemonStatus(arm_pb2.Empty())

    assert len(limits.joint_limits) == 6
    assert limits.joint_limits[1].pos_min == pytest.approx(-0.1)
    assert not limits.hardware_limits_enabled
    assert status.sim
    assert status.hardware_connected
