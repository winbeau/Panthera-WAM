from __future__ import annotations

import asyncio
from dataclasses import dataclass

import grpc
import pytest
from panthera_arm import arm_pb2, arm_pb2_grpc

from armd.backend import SimBackend
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer
from armd.state_tap import StateTap, StateTapDataLoss


@dataclass(frozen=True)
class Sample:
    sequence: int


def test_state_tap_supports_independent_readers_and_reports_overflow() -> None:
    tap: StateTap[Sample] = StateTap(capacity=3)
    for sequence in range(1, 4):
        tap.publish(Sample(sequence))

    assert tap.read_after(0, timeout=0).sequence == 1
    assert tap.read_after(1, timeout=0).sequence == 2
    assert tap.read_after(0, timeout=0).sequence == 1
    assert tap.get(2) == Sample(2)
    assert tap.get(0) is None
    assert tap.get(5) is None

    tap.publish(Sample(4))
    stats = tap.stats()
    assert stats.oldest_sequence == 2
    assert stats.newest_sequence == 4
    assert stats.overwritten_samples_total == 1
    assert tap.get(1) is None
    assert tap.get(4) == Sample(4)
    with pytest.raises(StateTapDataLoss) as error:
        tap.read_after(0, timeout=0)
    assert error.value.requested_sequence == 1
    assert error.value.oldest_available_sequence == 2


def test_hardware_loop_publishes_contiguous_measured_samples() -> None:
    loop = HardwareLoop(SimBackend, control_hz=100.0)
    loop.start()
    try:
        assert loop.wait_for_cycles(5)
        stats = loop.state_tap.stats()
        samples = []
        after = 0
        while after < stats.newest_sequence:
            sample = loop.state_tap.read_after(after, timeout=0)
            assert sample is not None
            samples.append(sample)
            after = sample.sequence
        assert [sample.sequence for sample in samples] == list(range(1, len(samples) + 1))
        assert all(len(sample.motors) == 7 for sample in samples)
        assert all(
            left.sampled_monotonic_ns < right.sampled_monotonic_ns
            for left, right in zip(samples, samples[1:])
        )
        assert len({sample.stream_instance_id for sample in samples}) == 1
        latest = loop.latest_state()
        assert latest is not None
        assert loop.state_tap.read_after(latest.sequence - 1, timeout=0) is latest
    finally:
        loop.stop()


@pytest.mark.asyncio
async def test_measured_state_grpc_stream_is_contiguous_and_timestamped() -> None:
    hardware_loop = HardwareLoop(SimBackend, control_hz=200.0)
    server = ArmdServer(hardware_loop, bind="127.0.0.1:0")
    hardware_loop.start()
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    call = stub.StreamMeasuredState(arm_pb2.StreamMeasuredStateRequest(start_at_latest=True))
    try:
        samples = [await asyncio.wait_for(call.read(), timeout=1.0) for _ in range(5)]
        sequences = [sample.state.sequence for sample in samples]
        timestamps = [sample.state.sampled_monotonic_ns for sample in samples]
        assert sequences == list(range(sequences[0], sequences[0] + 5))
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == len(timestamps)
        assert all(len(sample.state.joint.joints) == 6 for sample in samples)
        assert all(sample.state.gripper.state.valid for sample in samples)
        assert len({sample.stream_instance_id for sample in samples}) == 1
    finally:
        call.cancel()
        await channel.close()
        await server.stop()
        hardware_loop.stop()


@pytest.mark.asyncio
async def test_measured_state_grpc_stream_surfaces_ring_data_loss() -> None:
    hardware_loop = HardwareLoop(SimBackend, control_hz=200.0, state_tap_capacity=2)
    server = ArmdServer(hardware_loop, bind="127.0.0.1:0")
    hardware_loop.start()
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    try:
        assert hardware_loop.wait_for_cycles(5)
        call = stub.StreamMeasuredState(arm_pb2.StreamMeasuredStateRequest(after_sequence=0))
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await call.read()
        assert error.value.code() is grpc.StatusCode.DATA_LOSS
    finally:
        await channel.close()
        await server.stop()
        hardware_loop.stop()
