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
async def v2_stack():
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
        yield loop, stub, metadata
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


@pytest.mark.asyncio
async def test_m5_dynamics_terms_and_friction_defaults(v2_stack) -> None:
    _, stub, _ = v2_stack
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
    loop, stub, metadata = v2_stack
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
