"""Latest-only Pi policy gateway with explicit shadow/preview/apply modes."""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from panthera_arm import arm_pb2


class PolicyGatewayError(RuntimeError):
    pass


class GatewayMode(str, enum.Enum):
    SHADOW = "shadow"
    PREVIEW = "preview"
    SIM = "sim"
    HARDWARE = "hardware"


@dataclass(frozen=True, slots=True)
class PolicyObservation:
    request_id: str
    session_id: str
    task_id: str
    canonical_prompt: str
    state_sequence: int
    state_sampled_monotonic_ns: int
    state_stream_instance_id: str
    deadline_pi_monotonic_ns: int
    state_position: tuple[float, ...]
    overhead_rgb_jpeg: bytes
    wrist_rgb_jpeg: bytes
    overhead_capture_monotonic_ns: int
    wrist_capture_monotonic_ns: int

    def __post_init__(self) -> None:
        if not self.request_id or not self.session_id:
            raise ValueError("request_id and session_id are required")
        if not self.task_id or not self.canonical_prompt:
            raise ValueError("registered task_id and canonical_prompt are required")
        state = np.asarray(self.state_position, dtype=np.float64)
        if state.shape != (7,) or not np.isfinite(state).all():
            raise ValueError("state_position must contain seven finite values")
        if not self.overhead_rgb_jpeg or not self.wrist_rgb_jpeg:
            raise ValueError("both RGB observations are required")


@dataclass(frozen=True, slots=True)
class PolicyInferenceResponse:
    request_id: str
    session_id: str
    observation_sequence: int
    observation_sampled_monotonic_ns: int
    state_stream_instance_id: str
    deadline_pi_monotonic_ns: int
    waypoint_positions: tuple[tuple[float, ...], ...]
    step_offsets_ns: tuple[int, ...]
    checkpoint_sha256: str
    stats_sha256: str
    schema_sha256: str
    server_elapsed_ns: int

    def validate_for(self, observation: PolicyObservation) -> None:
        echoed = (
            ("request_id", self.request_id, observation.request_id),
            ("session_id", self.session_id, observation.session_id),
            ("observation_sequence", self.observation_sequence, observation.state_sequence),
            (
                "observation_sampled_monotonic_ns",
                self.observation_sampled_monotonic_ns,
                observation.state_sampled_monotonic_ns,
            ),
            (
                "state_stream_instance_id",
                self.state_stream_instance_id,
                observation.state_stream_instance_id,
            ),
            (
                "deadline_pi_monotonic_ns",
                self.deadline_pi_monotonic_ns,
                observation.deadline_pi_monotonic_ns,
            ),
        )
        for field, actual, expected in echoed:
            if actual != expected:
                raise PolicyGatewayError(f"policy response {field} did not echo the Pi request")
        positions = np.asarray(self.waypoint_positions, dtype=np.float64)
        if positions.ndim != 2 or positions.shape[1] != 7 or len(positions) == 0:
            raise PolicyGatewayError("policy response waypoints must have shape [T,7]")
        if not np.isfinite(positions).all():
            raise PolicyGatewayError("policy response contains NaN or Inf")
        offsets = np.asarray(self.step_offsets_ns, dtype=np.int64)
        if offsets.shape != (len(positions),) or offsets[0] <= 0 or np.any(np.diff(offsets) <= 0):
            raise PolicyGatewayError("policy response step offsets must be positive and increasing")
        for field, digest in (
            ("checkpoint_sha256", self.checkpoint_sha256),
            ("stats_sha256", self.stats_sha256),
            ("schema_sha256", self.schema_sha256),
        ):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise PolicyGatewayError(f"policy response {field} is not a lowercase SHA-256")
        if self.server_elapsed_ns < 0:
            raise PolicyGatewayError("policy response server_elapsed_ns must be non-negative")

    def to_proto(self, *, operator_confirmation_id: str = "") -> arm_pb2.PolicyActionChunk:
        return arm_pb2.PolicyActionChunk(
            request_id=self.request_id,
            session_id=self.session_id,
            observation_sequence=self.observation_sequence,
            observation_sampled_monotonic_ns=self.observation_sampled_monotonic_ns,
            state_stream_instance_id=self.state_stream_instance_id,
            deadline_pi_monotonic_ns=self.deadline_pi_monotonic_ns,
            waypoints=[
                arm_pb2.PolicyWaypoint(positions=positions, step_offset_ns=offset)
                for positions, offset in zip(
                    self.waypoint_positions,
                    self.step_offsets_ns,
                    strict=True,
                )
            ],
            checkpoint_sha256=self.checkpoint_sha256,
            stats_sha256=self.stats_sha256,
            schema_sha256=self.schema_sha256,
            server_elapsed_ns=self.server_elapsed_ns,
            operator_confirmation_id=operator_confirmation_id,
        )


@dataclass(frozen=True, slots=True)
class GatewayResult:
    mode: GatewayMode
    applied: bool
    accepted: bool
    execution_id: str = ""
    reject_reason: str = ""


class InferenceClient(Protocol):
    async def infer(self, observation: PolicyObservation) -> PolicyInferenceResponse: ...


class ArmPolicyStub(Protocol):
    async def ApplyPolicyChunk(self, request, *, metadata): ...


class PolicyGateway:
    """Rejects overlapping inference instead of queuing stale observations."""

    def __init__(self) -> None:
        self._inference_lock = asyncio.Lock()

    async def run(
        self,
        observation: PolicyObservation,
        *,
        inference_client: InferenceClient,
        mode: GatewayMode,
        arm_stub: ArmPolicyStub | None = None,
        lease_metadata=(),
        operator_confirmation_id: str = "",
    ) -> GatewayResult:
        if self._inference_lock.locked():
            raise PolicyGatewayError("a policy inference is already in flight; stale request was dropped")
        remaining_s = (observation.deadline_pi_monotonic_ns - time.monotonic_ns()) / 1e9
        if remaining_s <= 0:
            raise PolicyGatewayError("Pi inference deadline already expired")
        async with self._inference_lock:
            try:
                response = await asyncio.wait_for(
                    inference_client.infer(observation),
                    timeout=remaining_s,
                )
            except TimeoutError as exc:
                raise PolicyGatewayError("policy inference missed the Pi deadline") from exc
        response.validate_for(observation)
        if time.monotonic_ns() >= observation.deadline_pi_monotonic_ns:
            raise PolicyGatewayError("policy response arrived after the Pi deadline")
        if mode in {GatewayMode.SHADOW, GatewayMode.PREVIEW}:
            return GatewayResult(mode=mode, applied=False, accepted=True)
        if arm_stub is None:
            raise PolicyGatewayError("an armd stub is required for sim/hardware apply modes")
        if mode is GatewayMode.HARDWARE and not operator_confirmation_id:
            raise PolicyGatewayError("hardware mode requires a fresh operator confirmation id")
        applied = await arm_stub.ApplyPolicyChunk(
            response.to_proto(operator_confirmation_id=operator_confirmation_id),
            metadata=lease_metadata,
        )
        return GatewayResult(
            mode=mode,
            applied=bool(applied.accepted),
            accepted=bool(applied.accepted),
            execution_id=applied.execution_id,
            reject_reason=applied.reject_reason,
        )
