"""Latest-only Pi policy gateway and one-shot FastWAM HTTP client."""

from __future__ import annotations

import asyncio
import base64
import enum
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from panthera_arm import arm_pb2

from .policy_assets import PolicyAssetAllowList


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
        if self.state_sequence <= 0 or self.state_sampled_monotonic_ns <= 0:
            raise ValueError("state sequence and sampled monotonic timestamp must be positive")
        if not self.state_stream_instance_id:
            raise ValueError("state stream instance id is required")
        if self.deadline_pi_monotonic_ns <= 0:
            raise ValueError("Pi deadline must be positive")
        if self.overhead_capture_monotonic_ns <= 0 or self.wrist_capture_monotonic_ns <= 0:
            raise ValueError("both camera capture timestamps must be positive")
        if (
            max(self.overhead_capture_monotonic_ns, self.wrist_capture_monotonic_ns)
            > self.deadline_pi_monotonic_ns
        ):
            raise ValueError("camera capture timestamp cannot be after the Pi deadline")
        state = np.asarray(self.state_position, dtype=np.float64)
        if state.shape != (7,) or not np.isfinite(state).all():
            raise ValueError("state_position must contain seven finite values")
        if not _is_jpeg(self.overhead_rgb_jpeg) or not _is_jpeg(self.wrist_rgb_jpeg):
            raise ValueError("both RGB observations must be complete JPEG images")

    def to_http_mapping(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "canonical_prompt": self.canonical_prompt,
            "observation_sequence": self.state_sequence,
            "observation_sampled_monotonic_ns": self.state_sampled_monotonic_ns,
            "state_stream_instance_id": self.state_stream_instance_id,
            "deadline_pi_monotonic_ns": self.deadline_pi_monotonic_ns,
            "state_position": list(self.state_position),
            "overhead_rgb_jpeg_base64": base64.b64encode(self.overhead_rgb_jpeg).decode("ascii"),
            "wrist_rgb_jpeg_base64": base64.b64encode(self.wrist_rgb_jpeg).decode("ascii"),
            "overhead_capture_monotonic_ns": self.overhead_capture_monotonic_ns,
            "wrist_capture_monotonic_ns": self.wrist_capture_monotonic_ns,
        }


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

    @classmethod
    def from_mapping(cls, value: Any) -> "PolicyInferenceResponse":
        if not isinstance(value, dict):
            raise PolicyGatewayError("policy HTTP response must be a JSON object")
        try:
            result = cls(
                request_id=_required_string(value, "request_id"),
                session_id=_required_string(value, "session_id"),
                observation_sequence=_required_integer(value, "observation_sequence"),
                observation_sampled_monotonic_ns=_required_integer(
                    value,
                    "observation_sampled_monotonic_ns",
                ),
                state_stream_instance_id=_required_string(value, "state_stream_instance_id"),
                deadline_pi_monotonic_ns=_required_integer(value, "deadline_pi_monotonic_ns"),
                waypoint_positions=_waypoint_positions(value.get("waypoint_positions")),
                step_offsets_ns=_step_offsets(value.get("step_offsets_ns")),
                checkpoint_sha256=_required_string(value, "checkpoint_sha256"),
                stats_sha256=_required_string(value, "stats_sha256"),
                schema_sha256=_required_string(value, "schema_sha256"),
                server_elapsed_ns=_required_integer(
                    value,
                    "server_elapsed_ns",
                    positive=False,
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise PolicyGatewayError(f"malformed policy HTTP response: {exc}") from exc
        result._validate_shape_and_identity()
        return result

    def _validate_shape_and_identity(self) -> None:
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

    def validate_for(
        self,
        observation: PolicyObservation,
        *,
        asset_allow_list: PolicyAssetAllowList | None = None,
    ) -> None:
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
        self._validate_shape_and_identity()
        if asset_allow_list is not None and not asset_allow_list.allows(
            checkpoint_sha256=self.checkpoint_sha256,
            stats_sha256=self.stats_sha256,
            schema_sha256=self.schema_sha256,
        ):
            raise PolicyGatewayError("policy response assets are not in the deployment allow-list")

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
    response: PolicyInferenceResponse | None = None
    round_trip_ns: int = 0


class InferenceClient(Protocol):
    async def infer(self, observation: PolicyObservation) -> PolicyInferenceResponse: ...


class ArmPolicyStub(Protocol):
    async def ApplyPolicyChunk(self, request, *, metadata): ...


class PolicyHTTPClient:
    """One request, no retry, no queue FastWAM client."""

    def __init__(
        self,
        endpoint: str,
        *,
        max_response_bytes: int = 1 << 20,
        max_error_bytes: int = 4096,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("policy endpoint must be an http(s) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("policy endpoint must not contain credentials, query, or fragment")
        base_path = parsed.path.rstrip("/")
        self.infer_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, f"{base_path}/v1/infer", "", "")
        )
        if max_response_bytes <= 0 or max_error_bytes <= 0:
            raise ValueError("HTTP response bounds must be positive")
        self.max_response_bytes = int(max_response_bytes)
        self.max_error_bytes = int(max_error_bytes)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    async def infer(self, observation: PolicyObservation) -> PolicyInferenceResponse:
        remaining_s = (observation.deadline_pi_monotonic_ns - time.monotonic_ns()) / 1e9
        if remaining_s <= 0:
            raise PolicyGatewayError("Pi inference deadline already expired")
        return await asyncio.to_thread(self._infer_sync, observation, remaining_s)

    def _infer_sync(
        self,
        observation: PolicyObservation,
        timeout_s: float,
    ) -> PolicyInferenceResponse:
        body = json.dumps(
            observation.to_http_mapping(),
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.infer_url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Connection": "close",
            },
        )
        try:
            with self._opener.open(request, timeout=max(timeout_s, 0.001)) as response:
                if response.status != 200:
                    raise PolicyGatewayError(f"policy HTTP server returned status {response.status}")
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise PolicyGatewayError(
                        f"policy HTTP response content type must be application/json, got {content_type!r}"
                    )
                payload = _bounded_read(response, self.max_response_bytes)
        except urllib.error.HTTPError as exc:
            detail = _bounded_error(exc, self.max_error_bytes)
            if exc.code == 429:
                raise PolicyGatewayError("policy server is busy; observation was dropped") from exc
            raise PolicyGatewayError(f"policy HTTP request failed with status {exc.code}: {detail}") from exc
        except PolicyGatewayError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PolicyGatewayError(f"policy HTTP request failed: {exc}") from exc
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyGatewayError("policy HTTP response is not valid UTF-8 JSON") from exc
        return PolicyInferenceResponse.from_mapping(value)


class PolicyGateway:
    """Rejects overlapping inference instead of queuing stale observations."""

    def __init__(self, *, asset_allow_list: PolicyAssetAllowList | None = None) -> None:
        self._inference_lock = asyncio.Lock()
        self._asset_allow_list = asset_allow_list

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
        started_ns = time.monotonic_ns()
        async with self._inference_lock:
            try:
                response = await asyncio.wait_for(
                    inference_client.infer(observation),
                    timeout=remaining_s,
                )
            except TimeoutError as exc:
                raise PolicyGatewayError("policy inference missed the Pi deadline") from exc
        round_trip_ns = time.monotonic_ns() - started_ns
        response.validate_for(observation, asset_allow_list=self._asset_allow_list)
        if time.monotonic_ns() >= observation.deadline_pi_monotonic_ns:
            raise PolicyGatewayError("policy response arrived after the Pi deadline")
        if mode in {GatewayMode.SHADOW, GatewayMode.PREVIEW}:
            return GatewayResult(
                mode=mode,
                applied=False,
                accepted=True,
                response=response,
                round_trip_ns=round_trip_ns,
            )
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
            response=response,
            round_trip_ns=round_trip_ns,
        )


def _is_jpeg(value: bytes) -> bool:
    return len(value) >= 4 and value.startswith(b"\xff\xd8") and value.endswith(b"\xff\xd9")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _required_string(value: dict[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{field} must be a non-empty string")
    return result


def _required_integer(
    value: dict[str, Any],
    field: str,
    *,
    positive: bool = True,
) -> int:
    result = value.get(field)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValueError(f"{field} must be an integer")
    if positive and result <= 0:
        raise ValueError(f"{field} must be positive")
    if not positive and result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _waypoint_positions(value: Any) -> tuple[tuple[float, ...], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("waypoint_positions must be a nonempty JSON array")
    result = []
    for index, waypoint in enumerate(value):
        if not isinstance(waypoint, list) or len(waypoint) != 7:
            raise ValueError(f"waypoint_positions[{index}] must contain seven numbers")
        converted = []
        for item in waypoint:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"waypoint_positions[{index}] must contain JSON numbers")
            number = float(item)
            if not np.isfinite(number):
                raise ValueError(f"waypoint_positions[{index}] must be finite")
            converted.append(number)
        result.append(tuple(converted))
    return tuple(result)


def _step_offsets(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("step_offsets_ns must be a nonempty JSON array")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("step_offsets_ns must contain JSON integers")
        result.append(item)
    return tuple(result)


def _bounded_read(response, maximum: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError as exc:
            raise PolicyGatewayError("policy HTTP response has invalid Content-Length") from exc
        if length < 0 or length > maximum:
            raise PolicyGatewayError("policy HTTP response exceeded the configured size limit")
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise PolicyGatewayError("policy HTTP response exceeded the configured size limit")
    return payload


def _bounded_error(exc: urllib.error.HTTPError, maximum: int) -> str:
    payload = exc.read(maximum + 1)
    if len(payload) > maximum:
        payload = payload[:maximum]
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.decode("utf-8", errors="replace")
    if isinstance(value, dict) and isinstance(value.get("error"), str):
        return value["error"]
    return str(value)
