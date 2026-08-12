"""Read-only shadow/preview FastWAM policy gateway command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import grpc
import numpy as np
from panthera_arm import arm_pb2, arm_pb2_grpc, camera_pb2, camera_pb2_grpc

from .policy_assets import PolicyAssetAllowList, PolicyAssetError
from .policy_gateway import (
    GatewayMode,
    PolicyGateway,
    PolicyGatewayError,
    PolicyHTTPClient,
    PolicyObservation,
)

GRPC_OPTIONS = (
    ("grpc.enable_http_proxy", 0),
    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
    ("grpc.max_send_message_length", 16 * 1024 * 1024),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture one Pi observation and run a read-only FastWAM shadow/preview request"
    )
    parser.add_argument("--mode", choices=("shadow", "preview"), default="shadow")
    parser.add_argument(
        "--policy-endpoint",
        default=os.environ.get("PANTHERA_FASTWAM_ENDPOINT", "http://127.0.0.1:8080"),
    )
    parser.add_argument(
        "--arm-endpoint",
        default=os.environ.get("PANTHERA_ARM_ENDPOINT", "127.0.0.1:50051"),
    )
    parser.add_argument(
        "--overhead-endpoint",
        default=os.environ.get("PANTHERA_OVERHEAD_CAMERA_ENDPOINT", "127.0.0.1:50053"),
    )
    parser.add_argument(
        "--wrist-endpoint",
        default=os.environ.get("PANTHERA_CAMERA_ENDPOINT", "127.0.0.1:50052"),
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--canonical-prompt", required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--deadline-ms", type=int, default=1000)
    parser.add_argument("--camera-timeout-ms", type=int, default=1000)
    parser.add_argument("--max-state-age-ms", type=int, default=100)
    parser.add_argument("--max-camera-age-ms", type=int, default=250)
    parser.add_argument("--max-camera-skew-ms", type=int, default=100)
    parser.add_argument("--asset-allow-list", type=Path)
    parser.add_argument("--evidence-jsonl", type=Path)
    return parser


async def run(args: argparse.Namespace) -> dict:
    for field in (
        "deadline_ms",
        "camera_timeout_ms",
        "max_state_age_ms",
        "max_camera_age_ms",
        "max_camera_skew_ms",
    ):
        if getattr(args, field) <= 0:
            raise PolicyGatewayError(f"{field.replace('_', '-')} must be positive")
    if args.asset_allow_list is None:
        raise PolicyGatewayError("--asset-allow-list is required for shadow/preview validation")
    try:
        allow_list = PolicyAssetAllowList.load(args.asset_allow_list)
    except PolicyAssetError as exc:
        raise PolicyGatewayError(str(exc)) from exc
    observation = await capture_observation(args)
    gateway = PolicyGateway(asset_allow_list=allow_list)
    result = await gateway.run(
        observation,
        inference_client=PolicyHTTPClient(args.policy_endpoint),
        mode=GatewayMode(args.mode),
    )
    response = result.response
    if response is None:  # defensive: read-only modes always carry the validated prediction
        raise PolicyGatewayError("policy gateway returned no validated response")
    payload = {
        "schema_version": 1,
        "mode": result.mode.value,
        "accepted": result.accepted,
        "applied": result.applied,
        "request_id": observation.request_id,
        "session_id": observation.session_id,
        "task_id": observation.task_id,
        "canonical_prompt": observation.canonical_prompt,
        "observation_sequence": observation.state_sequence,
        "observation_sampled_monotonic_ns": observation.state_sampled_monotonic_ns,
        "state_stream_instance_id": observation.state_stream_instance_id,
        "deadline_pi_monotonic_ns": observation.deadline_pi_monotonic_ns,
        "state_position": list(observation.state_position),
        "overhead_capture_monotonic_ns": observation.overhead_capture_monotonic_ns,
        "wrist_capture_monotonic_ns": observation.wrist_capture_monotonic_ns,
        "waypoint_positions": [list(item) for item in response.waypoint_positions],
        "step_offsets_ns": list(response.step_offsets_ns),
        "checkpoint_sha256": response.checkpoint_sha256,
        "stats_sha256": response.stats_sha256,
        "schema_sha256": response.schema_sha256,
        "server_elapsed_ns": response.server_elapsed_ns,
        "round_trip_ns": result.round_trip_ns,
        "motion_rpc_called": False,
        "lease_acquired": False,
    }
    if args.evidence_jsonl is not None:
        _append_jsonl(args.evidence_jsonl, payload)
    return payload


async def capture_observation(args: argparse.Namespace) -> PolicyObservation:
    channels = (
        grpc.aio.insecure_channel(args.arm_endpoint, options=GRPC_OPTIONS),
        grpc.aio.insecure_channel(args.overhead_endpoint, options=GRPC_OPTIONS),
        grpc.aio.insecure_channel(args.wrist_endpoint, options=GRPC_OPTIONS),
    )
    arm_channel, overhead_channel, wrist_channel = channels
    try:
        arm_stub = arm_pb2_grpc.ArmServiceStub(arm_channel)
        overhead_stub = camera_pb2_grpc.CameraServiceStub(overhead_channel)
        wrist_stub = camera_pb2_grpc.CameraServiceStub(wrist_channel)
        timeout_s = args.camera_timeout_ms / 1000.0
        overhead_status, wrist_status = await asyncio.gather(
            overhead_stub.GetStatus(camera_pb2.CameraStatusRequest(), timeout=timeout_s),
            wrist_stub.GetStatus(camera_pb2.CameraStatusRequest(), timeout=timeout_s),
        )
        _validate_status(overhead_status, camera_pb2.CAMERA_DEVICE_ROLE_OVERHEAD, "overhead")
        _validate_status(wrist_status, camera_pb2.CAMERA_DEVICE_ROLE_WRIST, "wrist")
        state, overhead, wrist = await asyncio.gather(
            _latest_state(arm_stub, timeout_s),
            overhead_stub.CaptureFrame(
                camera_pb2.CaptureFrameRequest(
                    stream=camera_pb2.CAMERA_STREAM_TYPE_COLOR,
                    timeout_ms=args.camera_timeout_ms,
                ),
                timeout=timeout_s + 0.1,
            ),
            wrist_stub.CaptureFrame(
                camera_pb2.CaptureFrameRequest(
                    stream=camera_pb2.CAMERA_STREAM_TYPE_COLOR,
                    timeout_ms=args.camera_timeout_ms,
                ),
                timeout=timeout_s + 0.1,
            ),
        )
    except grpc.aio.AioRpcError as exc:
        raise PolicyGatewayError(f"observation gRPC failed: {exc.code().name}: {exc.details()}") from exc
    finally:
        await asyncio.gather(*(channel.close() for channel in channels))

    _validate_frame(overhead, camera_pb2.CAMERA_DEVICE_ROLE_OVERHEAD, "overhead")
    _validate_frame(wrist, camera_pb2.CAMERA_DEVICE_ROLE_WRIST, "wrist")
    motors = [*state.joint.joints, state.gripper.state]
    if len(motors) != 7 or not all(motor.valid for motor in motors):
        raise PolicyGatewayError("measured robot state must contain seven valid motors")
    if state.estop_engaged:
        raise PolicyGatewayError("E-Stop is engaged; shadow/preview observation rejected")
    now_ns = time.monotonic_ns()
    if state.sequence <= 0 or state.sampled_monotonic_ns <= 0 or not state.stream_instance_id:
        raise PolicyGatewayError("measured robot state is missing identity or monotonic timestamp")
    state_age_ns = now_ns - state.sampled_monotonic_ns
    if state_age_ns < 0 or state_age_ns > args.max_state_age_ms * 1_000_000:
        raise PolicyGatewayError("measured robot state is stale")
    overhead_ns = _frame_capture_ns(overhead)
    wrist_ns = _frame_capture_ns(wrist)
    for role, timestamp in (("overhead", overhead_ns), ("wrist", wrist_ns)):
        age_ns = now_ns - timestamp
        if age_ns < 0 or age_ns > args.max_camera_age_ms * 1_000_000:
            raise PolicyGatewayError(f"{role} camera frame is stale")
    if abs(overhead_ns - wrist_ns) > args.max_camera_skew_ms * 1_000_000:
        raise PolicyGatewayError("camera frame skew exceeds the configured bound")
    deadline_ns = now_ns + args.deadline_ms * 1_000_000
    return PolicyObservation(
        request_id=uuid.uuid4().hex,
        session_id=args.session_id or uuid.uuid4().hex,
        task_id=args.task_id,
        canonical_prompt=args.canonical_prompt,
        state_sequence=int(state.sequence),
        state_sampled_monotonic_ns=int(state.sampled_monotonic_ns),
        state_stream_instance_id=state.stream_instance_id,
        deadline_pi_monotonic_ns=deadline_ns,
        state_position=tuple(float(motor.position) for motor in motors),
        overhead_rgb_jpeg=_jpeg_bytes(overhead),
        wrist_rgb_jpeg=_jpeg_bytes(wrist),
        overhead_capture_monotonic_ns=overhead_ns,
        wrist_capture_monotonic_ns=wrist_ns,
    )


async def _latest_state(stub, timeout_s: float):
    stream = stub.StreamMeasuredState(
        arm_pb2.StreamMeasuredStateRequest(start_at_latest=True),
        timeout=timeout_s,
    )
    try:
        sample = await stream.read()
    finally:
        stream.cancel()
    if sample is grpc.aio.EOF:
        raise PolicyGatewayError("measured robot state stream ended without a sample")
    if sample is None:
        raise PolicyGatewayError("measured robot state stream returned no sample")
    return sample.state


def _validate_status(status, expected_role: int, label: str) -> None:
    if not status.available or not status.streaming:
        raise PolicyGatewayError(f"{label} camera is not available and streaming")
    if status.role != expected_role:
        raise PolicyGatewayError(f"{label} camera endpoint role mismatch")


def _validate_frame(frame, expected_role: int, label: str) -> None:
    if frame.role != expected_role:
        raise PolicyGatewayError(f"{label} camera frame role mismatch")
    if frame.stream != camera_pb2.CAMERA_STREAM_TYPE_COLOR:
        raise PolicyGatewayError(f"{label} camera frame is not color")
    if frame.sequence <= 0 or not frame.stream_instance_id:
        raise PolicyGatewayError(f"{label} camera frame is missing identity")
    if frame.timestamp_source == camera_pb2.CAMERA_TIMESTAMP_SOURCE_UNSPECIFIED:
        raise PolicyGatewayError(f"{label} camera timestamp source is unspecified")
    if frame.timestamp_quality == camera_pb2.CAMERA_TIMESTAMP_QUALITY_UNSPECIFIED:
        raise PolicyGatewayError(f"{label} camera timestamp quality is unspecified")
    if frame.pixel_format not in {
        camera_pb2.CAMERA_PIXEL_FORMAT_JPEG,
        camera_pb2.CAMERA_PIXEL_FORMAT_RGB8,
    }:
        raise PolicyGatewayError(f"{label} camera pixel format is unsupported")


def _frame_capture_ns(frame) -> int:
    if frame.HasField("estimated_capture_monotonic_ns"):
        value = int(frame.estimated_capture_monotonic_ns)
    else:
        value = int(frame.host_receive_monotonic_ns)
    if value <= 0:
        raise PolicyGatewayError("camera frame has no usable Pi monotonic capture timestamp")
    return value


def _jpeg_bytes(frame) -> bytes:
    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_JPEG:
        payload = bytes(frame.data)
    elif frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_RGB8:
        from io import BytesIO

        from PIL import Image

        stride = int(frame.stride or frame.width * 3)
        expected = stride * int(frame.height)
        if len(frame.data) != expected or stride < frame.width * 3:
            raise PolicyGatewayError("RGB8 camera payload does not match its geometry")
        array = np.frombuffer(frame.data, dtype=np.uint8).reshape(frame.height, stride)
        array = array[:, : frame.width * 3].reshape(frame.height, frame.width, 3)
        buffer = BytesIO()
        Image.fromarray(array).save(buffer, format="JPEG", quality=90)
        payload = buffer.getvalue()
    else:  # guarded by _validate_frame
        raise PolicyGatewayError("unsupported RGB camera pixel format")
    if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise PolicyGatewayError("camera JPEG payload is incomplete")
    return payload


def _append_jsonl(path: Path, payload: dict) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> None:
    args = build_parser().parse_args()
    try:
        payload = asyncio.run(run(args))
    except (PolicyGatewayError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
