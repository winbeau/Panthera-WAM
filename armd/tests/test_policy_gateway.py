from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from armd.policy_assets import PolicyAssetAllowList, PolicyAssetIdentity
from armd.policy_gateway import (
    GatewayMode,
    PolicyGateway,
    PolicyGatewayError,
    PolicyHTTPClient,
    PolicyInferenceResponse,
    PolicyObservation,
)

HASH = "c" * 64


def observation(*, request_id: str = "request-1", deadline_ns: int | None = None):
    return PolicyObservation(
        request_id=request_id,
        session_id="session-1",
        task_id="pick-red-block",
        canonical_prompt="pick up the red block",
        state_sequence=42,
        state_sampled_monotonic_ns=time.monotonic_ns() - 1_000_000,
        state_stream_instance_id="stream-1",
        deadline_pi_monotonic_ns=(time.monotonic_ns() + 500_000_000 if deadline_ns is None else deadline_ns),
        state_position=(0.0,) * 7,
        overhead_rgb_jpeg=b"\xff\xd8overhead\xff\xd9",
        wrist_rgb_jpeg=b"\xff\xd8wrist\xff\xd9",
        overhead_capture_monotonic_ns=time.monotonic_ns() - 2_000_000,
        wrist_capture_monotonic_ns=time.monotonic_ns() - 2_000_000,
    )


def response_for(value: PolicyObservation):
    return PolicyInferenceResponse(
        request_id=value.request_id,
        session_id=value.session_id,
        observation_sequence=value.state_sequence,
        observation_sampled_monotonic_ns=value.state_sampled_monotonic_ns,
        state_stream_instance_id=value.state_stream_instance_id,
        deadline_pi_monotonic_ns=value.deadline_pi_monotonic_ns,
        waypoint_positions=((0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01),),
        step_offsets_ns=(100_000_000,),
        checkpoint_sha256=HASH,
        stats_sha256=HASH,
        schema_sha256=HASH,
        server_elapsed_ns=1_000_000,
    )


class FakeInference:
    async def infer(self, value):
        return response_for(value)


class FakeArm:
    def __init__(self):
        self.requests = []

    async def ApplyPolicyChunk(self, request, *, metadata):
        self.requests.append((request, metadata))
        return SimpleNamespace(accepted=True, execution_id="execution-1", reject_reason="")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [GatewayMode.SHADOW, GatewayMode.PREVIEW])
async def test_shadow_and_preview_never_call_armd(mode):
    gateway = PolicyGateway()
    arm = FakeArm()
    result = await gateway.run(
        observation(),
        inference_client=FakeInference(),
        mode=mode,
        arm_stub=arm,
    )
    assert result.accepted and not result.applied
    assert arm.requests == []


@pytest.mark.asyncio
async def test_sim_apply_forwards_echoed_chunk_and_hardware_requires_confirmation():
    gateway = PolicyGateway()
    arm = FakeArm()
    value = observation()
    result = await gateway.run(
        value,
        inference_client=FakeInference(),
        mode=GatewayMode.SIM,
        arm_stub=arm,
        lease_metadata=(("x-panthera-lease", "token"),),
    )
    assert result.applied and result.execution_id == "execution-1"
    request, metadata = arm.requests[0]
    assert request.observation_sequence == value.state_sequence
    assert request.deadline_pi_monotonic_ns == value.deadline_pi_monotonic_ns
    assert request.operator_confirmation_id == ""
    assert metadata == (("x-panthera-lease", "token"),)

    with pytest.raises(PolicyGatewayError, match="operator confirmation"):
        await gateway.run(
            observation(request_id="request-2"),
            inference_client=FakeInference(),
            mode=GatewayMode.HARDWARE,
            arm_stub=arm,
        )


@pytest.mark.asyncio
async def test_gateway_rejects_response_echo_mismatch_and_expired_request():
    class MismatchInference:
        async def infer(self, value):
            response = response_for(value)
            return PolicyInferenceResponse(
                **{
                    field: getattr(response, field)
                    for field in response.__dataclass_fields__
                    if field != "request_id"
                },
                request_id="wrong",
            )

    with pytest.raises(PolicyGatewayError, match="request_id did not echo"):
        await PolicyGateway().run(
            observation(),
            inference_client=MismatchInference(),
            mode=GatewayMode.SHADOW,
        )
    with pytest.raises(PolicyGatewayError, match="already expired"):
        await PolicyGateway().run(
            observation(deadline_ns=time.monotonic_ns() - 1),
            inference_client=FakeInference(),
            mode=GatewayMode.SHADOW,
        )


@pytest.mark.asyncio
async def test_gateway_drops_overlapping_inference_instead_of_queueing():
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingInference:
        async def infer(self, value):
            started.set()
            await release.wait()
            return response_for(value)

    gateway = PolicyGateway()
    first = asyncio.create_task(
        gateway.run(
            observation(),
            inference_client=BlockingInference(),
            mode=GatewayMode.SHADOW,
        )
    )
    await started.wait()
    with pytest.raises(PolicyGatewayError, match="already in flight"):
        await gateway.run(
            observation(request_id="request-2"),
            inference_client=FakeInference(),
            mode=GatewayMode.SHADOW,
        )
    release.set()
    assert (await first).accepted


def test_http_client_rejects_credentials_and_builds_single_infer_url():
    with pytest.raises(ValueError, match="credentials"):
        PolicyHTTPClient("http://user:password@127.0.0.1:8080")
    client = PolicyHTTPClient("http://127.0.0.1:8080/base/")
    assert client.infer_url == "http://127.0.0.1:8080/base/v1/infer"


def test_http_response_parser_rejects_coerced_numbers_and_asset_mismatch():
    value = observation()
    payload = {
        "request_id": value.request_id,
        "session_id": value.session_id,
        "observation_sequence": value.state_sequence,
        "observation_sampled_monotonic_ns": value.state_sampled_monotonic_ns,
        "state_stream_instance_id": value.state_stream_instance_id,
        "deadline_pi_monotonic_ns": value.deadline_pi_monotonic_ns,
        "waypoint_positions": [[0.0] * 7],
        "step_offsets_ns": [100_000_000],
        "checkpoint_sha256": HASH,
        "stats_sha256": HASH,
        "schema_sha256": HASH,
        "server_elapsed_ns": 1,
    }
    parsed = PolicyInferenceResponse.from_mapping(payload)
    assert parsed.waypoint_positions == ((0.0,) * 7,)
    payload["step_offsets_ns"] = [1.5]
    with pytest.raises(PolicyGatewayError, match="JSON integers"):
        PolicyInferenceResponse.from_mapping(payload)


def test_gateway_rejects_response_outside_asset_allow_list():
    value = observation()
    response = response_for(value)
    allow_list = PolicyAssetAllowList((PolicyAssetIdentity("a" * 64, "b" * 64, "d" * 64),))
    with pytest.raises(PolicyGatewayError, match="allow-list"):
        response.validate_for(value, asset_allow_list=allow_list)
