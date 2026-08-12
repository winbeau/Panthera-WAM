from __future__ import annotations

import asyncio
import time

import grpc
import pytest
import pytest_asyncio
from panthera_arm import arm_pb2, arm_pb2_grpc

from armd.backend import SimBackend
from armd.control import LEASE_METADATA_KEY
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer

HASH = "b" * 64


@pytest_asyncio.fixture
async def policy_stack():
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(
        loop,
        bind="127.0.0.1:0",
        lease_timeout_s=2.0,
        watchdog_poll_s=0.02,
    )
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="policy-test"))
    metadata = ((LEASE_METADATA_KEY, acquired.lease_token),)
    try:
        yield loop, stub, metadata
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


def make_request(
    state: arm_pb2.RobotState,
    *,
    request_id: str = "request-1",
    session_id: str = "session-1",
    delta: float = 0.01,
    offset_ns: int = 100_000_000,
    deadline_ns: int | None = None,
) -> arm_pb2.PolicyActionChunk:
    positions = [joint.position for joint in state.joint.joints]
    positions.append(state.gripper.state.position)
    positions[0] += delta
    positions[6] += delta
    return arm_pb2.PolicyActionChunk(
        request_id=request_id,
        session_id=session_id,
        observation_sequence=state.sequence,
        observation_sampled_monotonic_ns=state.sampled_monotonic_ns,
        state_stream_instance_id=state.stream_instance_id,
        deadline_pi_monotonic_ns=(
            time.monotonic_ns() + 1_000_000_000 if deadline_ns is None else deadline_ns
        ),
        waypoints=[
            arm_pb2.PolicyWaypoint(
                positions=positions,
                step_offset_ns=offset_ns,
            )
        ],
        checkpoint_sha256=HASH,
        stats_sha256=HASH,
        schema_sha256=HASH,
        server_elapsed_ns=1_000_000,
    )


async def terminal_status(stub, execution_id: str):
    stream = stub.StreamExecution(arm_pb2.StreamExecutionRequest(execution_id=execution_id))
    async for status in stream:
        if status.state != arm_pb2.EXEC_STATE_RUNNING:
            return status
    raise AssertionError("execution stream ended without a terminal status")


@pytest.mark.asyncio
async def test_apply_policy_chunk_executes_in_sim_and_enforces_session_sequence(policy_stack):
    _, stub, metadata = policy_stack
    state = await stub.GetRobotState(arm_pb2.Empty())
    assert state.stream_instance_id
    accepted = await stub.ApplyPolicyChunk(make_request(state), metadata=metadata)
    assert accepted.accepted
    assert accepted.execution_id
    assert accepted.max_joint_delta == pytest.approx(0.01)
    assert accepted.max_gripper_delta == pytest.approx(0.01)
    done = await terminal_status(stub, accepted.execution_id)
    assert done.state == arm_pb2.EXEC_STATE_DONE
    assert done.fraction == 1.0
    acceptance = await stub.GetPolicyAcceptance(
        arm_pb2.PolicyAcceptanceRequest(execution_id=accepted.execution_id)
    )
    assert acceptance.terminal
    assert acceptance.passed
    assert acceptance.tolerance_m == pytest.approx(0.03)

    next_state = await stub.GetRobotState(arm_pb2.Empty())
    wrong_session = await stub.ApplyPolicyChunk(
        make_request(next_state, request_id="request-2", session_id="wrong-session"),
        metadata=metadata,
    )
    assert not wrong_session.accepted
    assert "session_id" in wrong_session.reject_reason

    duplicate = await stub.ApplyPolicyChunk(
        make_request(next_state, request_id="request-1"),
        metadata=metadata,
    )
    assert not duplicate.accepted
    assert "request_id" in duplicate.reject_reason

    accepted_second = await stub.ApplyPolicyChunk(
        make_request(next_state, request_id="request-2"),
        metadata=metadata,
    )
    assert accepted_second.accepted
    assert (await terminal_status(stub, accepted_second.execution_id)).state == arm_pb2.EXEC_STATE_DONE


@pytest.mark.asyncio
async def test_policy_acceptance_rejects_unknown_execution(policy_stack):
    _, stub, _ = policy_stack
    response = await stub.GetPolicyAcceptance(arm_pb2.PolicyAcceptanceRequest(execution_id="missing"))
    assert not response.terminal
    assert not response.passed
    assert "unknown" in response.reject_reason


@pytest.mark.asyncio
async def test_apply_policy_chunk_rejects_malformed_stale_and_unsafe_inputs(policy_stack):
    _, stub, metadata = policy_stack

    async def reject(mutator, expected: str):
        state = await stub.GetRobotState(arm_pb2.Empty())
        request = make_request(state, request_id=f"reject-{state.sequence}")
        mutator(request)
        response = await stub.ApplyPolicyChunk(request, metadata=metadata)
        assert not response.accepted
        assert expected in response.reject_reason

    await reject(lambda request: setattr(request, "checkpoint_sha256", "bad"), "SHA-256")
    await reject(
        lambda request: setattr(request, "state_stream_instance_id", "wrong"),
        "stream instance mismatch",
    )
    await reject(
        lambda request: setattr(request, "observation_sampled_monotonic_ns", 1),
        "does not match its measured-state sequence",
    )
    await reject(
        lambda request: setattr(request, "deadline_pi_monotonic_ns", 1),
        "deadline has expired",
    )

    def wrong_dimension(request):
        del request.waypoints[0].positions[:]
        request.waypoints[0].positions.extend([0.0] * 6)

    await reject(wrong_dimension, "shape [T,7]")

    def over_limit(request):
        request.waypoints[0].positions[0] = 3.0

    await reject(over_limit, "position soft limit")

    def excessive_velocity(request):
        request.waypoints[0].positions[0] = 0.5
        request.waypoints[0].step_offset_ns = 10_000_000

    await reject(excessive_velocity, "velocity limit")


@pytest.mark.asyncio
async def test_estop_cancels_active_policy_chunk(policy_stack):
    _, stub, metadata = policy_stack
    state = await stub.GetRobotState(arm_pb2.Empty())
    accepted = await stub.ApplyPolicyChunk(
        make_request(
            state,
            request_id="long-request",
            delta=0.1,
            offset_ns=1_000_000_000,
            deadline_ns=time.monotonic_ns() + 2_000_000_000,
        ),
        metadata=metadata,
    )
    assert accepted.accepted
    await asyncio.sleep(0.05)
    stopped = await stub.EStop(arm_pb2.EStopRequest(reason="policy fault injection"))
    assert stopped.engaged
    terminal = await terminal_status(stub, accepted.execution_id)
    assert terminal.state == arm_pb2.EXEC_STATE_CANCELLED
    assert "estop" in terminal.error_message
