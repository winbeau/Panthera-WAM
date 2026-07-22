from __future__ import annotations

import asyncio

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
async def kinematics_stack():
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(loop, bind="127.0.0.1:0", lease_timeout_s=60.0)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{server.port}", options=(("grpc.enable_http_proxy", 0),))
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="kinematics-test"))
    metadata = ((LEASE_METADATA_KEY, acquired.lease_token),)
    try:
        yield loop, stub, metadata
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


@pytest.mark.asyncio
async def test_kinematics_rpcs_and_ik_timeout(kinematics_stack) -> None:
    _, stub, _ = kinematics_stack
    reference = [0.2, 0.6, 0.8, 0.1, -0.2, 0.1]
    fk = await stub.GetForwardKinematics(arm_pb2.JointAnglesOptional(joint_angles=reference))
    jacobian = await stub.GetJacobian(arm_pb2.JointAnglesOptional(joint_angles=reference))
    manipulability = await stub.GetManipulability(arm_pb2.JointAnglesOptional(joint_angles=reference))
    assert len(fk.position) == 3
    assert len(fk.rotation_matrix) == 9
    assert (jacobian.rows, jacobian.cols, len(jacobian.matrix)) == (6, 6, 36)
    assert manipulability.mu >= 0.0

    target = arm_pb2.CartesianPose(
        position=fk.position,
        matrix=arm_pb2.RotationMatrix(values=fk.rotation_matrix),
    )
    solved = await stub.GetInverseKinematics(
        arm_pb2.InverseKinematicsRequest(
            target=target,
            init_q=reference,
            multi_init=False,
            num_attempts=1,
        ),
        timeout=5.0,
    )
    assert solved.found and not solved.timeout
    assert solved.error < 1e-4

    timed_out = await stub.GetInverseKinematics(
        arm_pb2.InverseKinematicsRequest(
            target=arm_pb2.CartesianPose(position=[1.5, 1.5, 1.5]),
            timeout_s=0.0,
        ),
        timeout=2.0,
    )
    assert not timed_out.found and timed_out.timeout


@pytest.mark.asyncio
async def test_plan_movel_stream_and_cancel(kinematics_stack) -> None:
    _, stub, metadata = kinematics_stack
    current_fk = await stub.GetForwardKinematics(arm_pb2.JointAnglesOptional())
    target_position = np.asarray(current_fk.position)
    target_position[2] += 0.004
    target = arm_pb2.CartesianPose(
        position=target_position.tolist(),
        matrix=arm_pb2.RotationMatrix(values=current_fk.rotation_matrix),
    )

    preview = await stub.PlanCartesianPath(
        arm_pb2.PlanCartesianPathRequest(waypoints=[target]),
        timeout=5.0,
    )
    assert preview.fraction == 1.0
    assert preview.joint_trajectory

    accepted = await stub.MoveL(
        arm_pb2.MoveLRequest(target=target, duration_s=0.3),
        metadata=metadata,
        timeout=5.0,
    )
    fractions = []
    states = []
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        fractions.append(status.fraction)
        states.append(status.state)
    assert all(later >= earlier for earlier, later in zip(fractions, fractions[1:], strict=False))
    assert states[-1] == arm_pb2.EXEC_STATE_DONE
    assert fractions[-1] == 1.0

    current_fk = await stub.GetForwardKinematics(arm_pb2.JointAnglesOptional())
    cancel_target = np.asarray(current_fk.position)
    cancel_target[2] += 0.006
    second = await stub.MoveL(
        arm_pb2.MoveLRequest(
            target=arm_pb2.CartesianPose(
                position=cancel_target.tolist(),
                matrix=arm_pb2.RotationMatrix(values=current_fk.rotation_matrix),
            ),
            duration_s=1.0,
        ),
        metadata=metadata,
        timeout=5.0,
    )
    await asyncio.sleep(0.1)
    cancelled = await stub.CancelExecution(
        arm_pb2.CancelExecutionRequest(execution_id=second.execution_id),
        metadata=metadata,
    )
    assert cancelled.cancelled
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=second.execution_id)
    ):
        final = status
    assert final.state == arm_pb2.EXEC_STATE_CANCELLED


@pytest.mark.asyncio
async def test_movel_stream_reports_failed_after_motor_disconnect(kinematics_stack) -> None:
    loop, stub, metadata = kinematics_stack
    current_fk = await stub.GetForwardKinematics(arm_pb2.JointAnglesOptional())
    target_position = np.asarray(current_fk.position)
    target_position[2] += 0.006
    accepted = await stub.MoveL(
        arm_pb2.MoveLRequest(
            target=arm_pb2.CartesianPose(
                position=target_position.tolist(),
                matrix=arm_pb2.RotationMatrix(values=current_fk.rotation_matrix),
            ),
            duration_s=1.0,
        ),
        metadata=metadata,
        timeout=5.0,
    )

    await asyncio.sleep(0.1)
    await asyncio.wrap_future(loop.submit(lambda backend: backend.set_motor_connected(1, False)))
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
    assert final is not None
    assert final.state == arm_pb2.EXEC_STATE_FAILED
    assert "状态无效" in final.error_message

    await asyncio.wrap_future(loop.submit(lambda backend: backend.set_motor_connected(1, True)))


@pytest.mark.asyncio
async def test_check_reached_uses_fresh_joint_state(kinematics_stack) -> None:
    _, stub, _ = kinematics_stack
    response = await stub.CheckReached(
        arm_pb2.CheckReachedRequest(target_positions=[0.0] * 6, tolerance=0.001)
    )
    assert response.reached
    assert max(response.errors) <= 0.001


@pytest.mark.asyncio
async def test_movel_rejects_duration_that_breaks_trajectory_limits(kinematics_stack) -> None:
    _, stub, metadata = kinematics_stack
    current_fk = await stub.GetForwardKinematics(arm_pb2.JointAnglesOptional())
    target_position = np.asarray(current_fk.position)
    target_position[2] += 0.004

    with pytest.raises(grpc.aio.AioRpcError) as error:
        await stub.MoveL(
            arm_pb2.MoveLRequest(
                target=arm_pb2.CartesianPose(
                    position=target_position.tolist(),
                    matrix=arm_pb2.RotationMatrix(values=current_fk.rotation_matrix),
                ),
                duration_s=0.001,
            ),
            metadata=metadata,
            timeout=5.0,
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert "duration_s 过短" in error.value.details()
