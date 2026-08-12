"""Independent gRPC collector for Pi-local state and camera services."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import grpc
import numpy as np
from panthera_arm import arm_pb2, arm_pb2_grpc, camera_pb2, camera_pb2_grpc
from PIL import Image

from .alignment import align_episode, estimate_aligned_camera_state_offset
from .clocks import AffineClockFit, fit_affine_clock
from .quality import build_sync_report, build_timestamp_quality, quality_gate_reasons
from .schema import ACTION_SEMANTICS, FPS, SCHEMA_VERSION, AlignedSample, CameraSample, StateSample
from .staging import AtomicEpisodeWriter


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    arm_endpoint: str
    overhead_endpoint: str
    wrist_endpoint: str
    collection_root: Path
    episode_id: str
    canonical_task: str
    operator: str
    panthera_wam_commit: str
    calibration: dict[str, Any]
    identity: dict[str, str]
    duration_s: float = 5.0
    capture_depth: bool = False
    allow_unapproved_root: bool = False
    expected_overhead_serial: str = ""
    expected_wrist_serial: str = ""
    aliases_zh: tuple[str, ...] = ()
    aliases_en: tuple[str, ...] = ()
    success: bool = True
    failure_reason: str | None = None
    grpc_options: tuple[tuple[str, int], ...] = (
        ("grpc.enable_http_proxy", 0),
        ("grpc.max_receive_message_length", 16 * 1024 * 1024),
        ("grpc.max_send_message_length", 16 * 1024 * 1024),
    )

    def __post_init__(self) -> None:
        if not 0 < self.duration_s <= 3600:
            raise ValueError("duration_s must be in (0, 3600]")
        if len(self.panthera_wam_commit) != 40:
            raise ValueError("panthera_wam_commit must be a full Git SHA")


@dataclass(slots=True)
class CaptureResult:
    states: list[StateSample] = field(default_factory=list)
    overhead_rgb: list[CameraSample] = field(default_factory=list)
    wrist_rgb: list[CameraSample] = field(default_factory=list)
    wrist_depth: list[CameraSample] = field(default_factory=list)


def _enum_name(module, field: str, value: int) -> str:
    return getattr(module, field).Name(value).lower()


def _state_sample(message: arm_pb2.MeasuredStateSample, overflow_baseline: int) -> StateSample:
    robot = message.state
    motors = [*robot.joint.joints, robot.gripper.state]
    if len(motors) != 7:
        raise ValueError(f"measured state must contain 7 motors, got {len(motors)}")
    return StateSample(
        sequence=int(robot.sequence),
        sampled_monotonic_ns=int(robot.sampled_monotonic_ns),
        position=tuple(float(motor.position) for motor in motors),
        velocity=tuple(float(motor.velocity) for motor in motors),
        torque=tuple(float(motor.torque) for motor in motors),
        valid=tuple(bool(motor.valid) for motor in motors),
        mode=tuple(int(motor.mode) for motor in motors),
        fault=tuple(int(motor.fault) for motor in motors),
        estop_engaged=bool(robot.estop_engaged),
        stream_instance_id=message.stream_instance_id,
        tap_overflow_count=int(message.overwritten_samples_total) - overflow_baseline,
        tap_oldest_available_sequence=int(message.oldest_available_sequence),
    )


def _write_camera_payload(frame: camera_pb2.CameraFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_JPEG:
        if not frame.data.startswith(b"\xff\xd8") or not frame.data.endswith(b"\xff\xd9"):
            raise ValueError("invalid JPEG camera frame")
        path.write_bytes(frame.data)
        return
    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_RGB8:
        row_bytes = frame.width * 3
        stride = frame.stride or row_bytes
        raw = np.frombuffer(frame.data, dtype=np.uint8)
        if raw.size != stride * frame.height:
            raise ValueError("RGB8 payload size does not match stride and dimensions")
        image = raw.reshape(frame.height, stride)[:, :row_bytes].reshape(frame.height, frame.width, 3)
        Image.fromarray(image).save(path, format="PNG")
        return
    if frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_Z16:
        row_bytes = frame.width * 2
        stride = frame.stride or row_bytes
        raw = np.frombuffer(frame.data, dtype=np.uint8)
        if raw.size != stride * frame.height:
            raise ValueError("Z16 payload size does not match stride and dimensions")
        packed = raw.reshape(frame.height, stride)[:, :row_bytes].copy()
        image = packed.view("<u2").reshape(frame.height, frame.width)
        Image.fromarray(image).save(path, format="PNG")
        return
    raise ValueError(f"unsupported camera pixel format: {frame.pixel_format}")


def _camera_sample(
    item: camera_pb2.CollectedCameraFrame,
    *,
    stream_name: str,
    path: Path,
    overflow_baseline: int,
) -> CameraSample:
    frame = item.frame
    estimated = (
        int(frame.estimated_capture_monotonic_ns)
        if frame.HasField("estimated_capture_monotonic_ns")
        else None
    )
    device_raw = float(frame.device_timestamp_raw)
    if frame.device_timestamp_unit == camera_pb2.CAMERA_TIMESTAMP_UNIT_UNSPECIFIED:
        device_raw = None
    return CameraSample(
        stream_name=stream_name,
        sequence=int(frame.sequence),
        stream_instance_id=frame.stream_instance_id,
        path=path,
        width=int(frame.width),
        height=int(frame.height),
        pixel_format=_enum_name(
            camera_pb2,
            "CameraPixelFormat",
            frame.pixel_format,
        ).removeprefix("camera_pixel_format_"),
        device_timestamp_raw=device_raw,
        device_timestamp_unit=_enum_name(
            camera_pb2,
            "CameraTimestampUnit",
            frame.device_timestamp_unit,
        ).removeprefix("camera_timestamp_unit_"),
        device_clock_domain=_enum_name(
            camera_pb2,
            "CameraClockDomain",
            frame.device_clock_domain,
        ).removeprefix("camera_clock_domain_"),
        host_receive_monotonic_ns=int(frame.host_receive_monotonic_ns),
        host_publish_monotonic_ns=int(frame.host_publish_monotonic_ns),
        estimated_capture_monotonic_ns=estimated,
        timestamp_source=_enum_name(
            camera_pb2,
            "CameraTimestampSource",
            frame.timestamp_source,
        ).removeprefix("camera_timestamp_source_"),
        timestamp_quality=_enum_name(
            camera_pb2,
            "CameraTimestampQuality",
            frame.timestamp_quality,
        ).removeprefix("camera_timestamp_quality_"),
        device_frame_number=int(frame.device_frame_number),
        frameset_sequence=int(frame.frameset_sequence),
        depth_scale=float(frame.depth_scale),
        ring_overflow_count=int(item.overwritten_samples_total) - overflow_baseline,
        ring_oldest_available_sequence=int(item.oldest_available_sequence),
    )


def _camera_extension(pixel_format: int) -> str:
    if pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_JPEG:
        return ".jpg"
    if pixel_format in (
        camera_pb2.CAMERA_PIXEL_FORMAT_RGB8,
        camera_pb2.CAMERA_PIXEL_FORMAT_Z16,
    ):
        return ".png"
    raise ValueError(f"unsupported camera pixel format: {pixel_format}")


async def _consume_states(
    stub: arm_pb2_grpc.ArmServiceStub,
    output: list[StateSample],
) -> None:
    baseline: int | None = None
    call = stub.StreamMeasuredState(arm_pb2.StreamMeasuredStateRequest(start_at_latest=True))
    async for message in call:
        if baseline is None:
            baseline = int(message.overwritten_samples_total)
        output.append(_state_sample(message, baseline))


async def _consume_camera(
    stub: camera_pb2_grpc.CameraServiceStub,
    *,
    stream: int,
    stream_name: str,
    raw_dir: Path,
    output: list[CameraSample],
) -> None:
    baseline: int | None = None
    call = stub.StreamCollectedFrames(
        camera_pb2.StreamCollectedFramesRequest(
            stream=stream,
            start_at_latest=True,
        )
    )
    async for item in call:
        if baseline is None:
            baseline = int(item.overwritten_samples_total)
        extension = _camera_extension(item.frame.pixel_format)
        path = raw_dir / stream_name / f"{int(item.frame.sequence):012d}{extension}"
        await asyncio.to_thread(_write_camera_payload, item.frame, path)
        output.append(
            _camera_sample(
                item,
                stream_name=stream_name,
                path=path,
                overflow_baseline=baseline,
            )
        )


def _map_device_clock(frames: list[CameraSample]) -> AffineClockFit | None:
    if len(frames) < 3 or all(frame.estimated_capture_monotonic_ns is not None for frame in frames):
        return None
    units = {frame.device_timestamp_unit for frame in frames}
    domains = {frame.device_clock_domain for frame in frames}
    if len(units) != 1 or len(domains) != 1:
        raise ValueError("camera device clock unit/domain changed within an episode")
    unit = next(iter(units))
    expected_scale = {
        "milliseconds": 1_000_000.0,
        "nanoseconds": 1.0,
    }.get(unit)
    if expected_scale is None or any(frame.device_timestamp_raw is None for frame in frames):
        return None
    fit = fit_affine_clock(
        [float(frame.device_timestamp_raw) for frame in frames],
        [frame.host_receive_monotonic_ns for frame in frames],
        expected_ns_per_device_unit=expected_scale,
    )
    frames[:] = [
        replace(
            frame,
            estimated_capture_monotonic_ns=fit.map(float(frame.device_timestamp_raw)),
            timestamp_source="device_to_host_estimate",
            timestamp_quality="estimated",
        )
        for frame in frames
    ]
    return fit


async def _camera_status(
    stub: camera_pb2_grpc.CameraServiceStub,
    *,
    expected_role: int,
    expected_serial: str,
) -> camera_pb2.CameraStatus:
    deadline = time.monotonic() + 5.0
    while True:
        status = await stub.GetStatus(camera_pb2.CameraStatusRequest())
        if status.available and status.streaming:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(status.error or "camera is not streaming")
        await asyncio.sleep(0.05)
    if status.role != expected_role:
        raise RuntimeError(f"camera role mismatch: expected={expected_role}, observed={status.role}")
    if expected_serial and status.serial != expected_serial:
        raise RuntimeError(f"camera serial mismatch: expected={expected_serial}, observed={status.serial}")
    return status


def _link_selected(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, destination)


def _staging_rows(writer: AtomicEpisodeWriter, aligned: list[AlignedSample]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sample in aligned:
        if (
            not sample.sync_ok
            or sample.state is None
            or sample.overhead_rgb is None
            or sample.wrist_rgb is None
        ):
            raise ValueError(f"cannot stage invalid aligned sample {sample.tick_index}")
        overhead_suffix = sample.overhead_rgb.path.suffix.lower()
        overhead_relative = f"overhead/{sample.tick_index:06d}{overhead_suffix}"
        wrist_relative = f"wrist_rgb/{sample.tick_index:06d}.png"
        _link_selected(sample.overhead_rgb.path, writer.path(overhead_relative))
        _link_selected(sample.wrist_rgb.path, writer.path(wrist_relative))
        row = {
            "tick_index": sample.tick_index,
            "tick_monotonic_ns": sample.tick_monotonic_ns,
            "state": {
                "position": list(sample.state.position),
                "velocity": list(sample.state.velocity),
                "sequence": sample.state.sequence,
                "sampled_monotonic_ns": sample.state.sampled_monotonic_ns,
                "interpolated": sample.state.interpolated,
                "freshness_ns": sample.state.freshness_ns,
            },
            "overhead_rgb": sample.overhead_rgb.staging_record(
                tick_monotonic_ns=sample.tick_monotonic_ns,
                relative_path=overhead_relative,
            ),
            "wrist_rgb": sample.wrist_rgb.staging_record(
                tick_monotonic_ns=sample.tick_monotonic_ns,
                relative_path=wrist_relative,
            ),
            "sync_ok": True,
            "sync_reasons": [],
        }
        if sample.wrist_depth is not None:
            depth_relative = f"wrist_depth/{sample.tick_index:06d}.png"
            _link_selected(sample.wrist_depth.path, writer.path(depth_relative))
            row["wrist_depth"] = {
                **sample.wrist_depth.staging_record(
                    tick_monotonic_ns=sample.tick_monotonic_ns,
                    relative_path=depth_relative,
                ),
                "depth_scale": sample.wrist_depth.depth_scale,
                "pixel_format": "Z16",
            }
        rows.append(row)
    return rows


async def collect_episode(config: CollectorConfig) -> Path:
    writer = AtomicEpisodeWriter(
        config.collection_root,
        config.episode_id,
        allow_unapproved_root=config.allow_unapproved_root,
    )
    raw_dir = writer.path("raw")
    result = CaptureResult()
    arm_channel = grpc.aio.insecure_channel(config.arm_endpoint, options=config.grpc_options)
    overhead_channel = grpc.aio.insecure_channel(config.overhead_endpoint, options=config.grpc_options)
    wrist_channel = grpc.aio.insecure_channel(config.wrist_endpoint, options=config.grpc_options)
    channels = (arm_channel, overhead_channel, wrist_channel)
    tasks: list[asyncio.Task[None]] = []
    started_wall_time = time.time_ns()
    started_monotonic_ns = time.monotonic_ns()
    try:
        await asyncio.gather(*(channel.channel_ready() for channel in channels))
        arm_stub = arm_pb2_grpc.ArmServiceStub(arm_channel)
        overhead_stub = camera_pb2_grpc.CameraServiceStub(overhead_channel)
        wrist_stub = camera_pb2_grpc.CameraServiceStub(wrist_channel)
        overhead_status, wrist_status = await asyncio.gather(
            _camera_status(
                overhead_stub,
                expected_role=camera_pb2.CAMERA_DEVICE_ROLE_OVERHEAD,
                expected_serial=config.expected_overhead_serial,
            ),
            _camera_status(
                wrist_stub,
                expected_role=camera_pb2.CAMERA_DEVICE_ROLE_WRIST,
                expected_serial=config.expected_wrist_serial,
            ),
        )
        tasks = [
            asyncio.create_task(_consume_states(arm_stub, result.states)),
            asyncio.create_task(
                _consume_camera(
                    overhead_stub,
                    stream=camera_pb2.CAMERA_STREAM_TYPE_COLOR,
                    stream_name="overhead_rgb",
                    raw_dir=raw_dir,
                    output=result.overhead_rgb,
                )
            ),
            asyncio.create_task(
                _consume_camera(
                    wrist_stub,
                    stream=camera_pb2.CAMERA_STREAM_TYPE_COLOR,
                    stream_name="wrist_rgb",
                    raw_dir=raw_dir,
                    output=result.wrist_rgb,
                )
            ),
        ]
        if config.capture_depth:
            tasks.append(
                asyncio.create_task(
                    _consume_camera(
                        wrist_stub,
                        stream=camera_pb2.CAMERA_STREAM_TYPE_DEPTH,
                        stream_name="wrist_depth",
                        raw_dir=raw_dir,
                        output=result.wrist_depth,
                    )
                )
            )
        await asyncio.sleep(config.duration_s)
        for task in tasks:
            task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError)
        ]
        if failures:
            raise RuntimeError(f"collector stream failed: {failures[0]}")

        result.states.sort(key=lambda item: item.sampled_monotonic_ns)
        result.overhead_rgb.sort(key=lambda item: item.alignment_monotonic_ns)
        result.wrist_rgb.sort(key=lambda item: item.alignment_monotonic_ns)
        result.wrist_depth.sort(key=lambda item: item.alignment_monotonic_ns)
        affine_fits = {}
        wrist_rgb_fit = _map_device_clock(result.wrist_rgb)
        if wrist_rgb_fit is not None:
            affine_fits["wrist_rgb"] = wrist_rgb_fit
        wrist_depth_fit = _map_device_clock(result.wrist_depth)
        if wrist_depth_fit is not None:
            affine_fits["wrist_depth"] = wrist_depth_fit
        result.wrist_rgb.sort(key=lambda item: item.alignment_monotonic_ns)
        result.wrist_depth.sort(key=lambda item: item.alignment_monotonic_ns)
        aligned = align_episode(
            states=result.states,
            overhead_rgb=result.overhead_rgb,
            wrist_rgb=result.wrist_rgb,
            wrist_depth=result.wrist_depth,
            require_depth=config.capture_depth,
        )
        camera_state_offset, offset_scores = estimate_aligned_camera_state_offset(aligned)
        sync_report = build_sync_report(
            states=result.states,
            overhead_rgb=result.overhead_rgb,
            wrist_rgb=result.wrist_rgb,
            wrist_depth=result.wrist_depth,
            aligned=aligned,
        )
        sync_report["camera_state_offset"] = {
            "selected_frames": camera_state_offset,
            "scores": {str(offset): score for offset, score in offset_scores.items()},
            "method": (
                "motion_correlation_offsets_minus2_to_plus2"
                if camera_state_offset is not None
                else "insufficient_motion"
            ),
        }
        timestamp_quality = build_timestamp_quality(
            states=result.states,
            overhead_rgb=result.overhead_rgb,
            wrist_rgb=result.wrist_rgb,
            wrist_depth=result.wrist_depth,
            affine_fits=affine_fits,
        )
        gate_reasons = quality_gate_reasons(sync_report, timestamp_quality)
        if gate_reasons:
            writer.abort("quality_gate_failure", details={"reasons": gate_reasons})
            raise ValueError(f"episode failed quality gates: {gate_reasons}")
        rows = _staging_rows(writer, aligned)
        shutil.rmtree(raw_dir)
        finished_wall_time = time.time_ns()
        finished_monotonic_ns = time.monotonic_ns()
        episode = {
            "episode_id": config.episode_id,
            "canonical_task": config.canonical_task,
            "aliases_zh": list(config.aliases_zh),
            "aliases_en": list(config.aliases_en),
            "operator": config.operator,
            "success": config.success,
            "failure_reason": config.failure_reason,
            "fps": FPS,
            "action_semantics": ACTION_SEMANTICS,
            "action_source": "next_state_pseudo_action",
            "schema_version": SCHEMA_VERSION,
            "camera_state_offset_frames": camera_state_offset,
            "offset_estimation_method": sync_report["camera_state_offset"]["method"],
            "started_wall_time_ns": started_wall_time,
            "finished_wall_time_ns": finished_wall_time,
            "started_monotonic_ns": started_monotonic_ns,
            "finished_monotonic_ns": finished_monotonic_ns,
            "panthera_wam_commit": config.panthera_wam_commit,
            "identity": config.identity,
            "robot": {"model": "Panthera-HT", "serial": ""},
            "cameras": {
                "overhead": {
                    "model": overhead_status.model,
                    "serial": overhead_status.serial,
                    "role": "overhead",
                },
                "wrist": {
                    "model": wrist_status.model,
                    "serial": wrist_status.serial,
                    "role": "wrist",
                },
            },
            "queue_policy": {
                "state_capacity": 4096,
                "camera_capacity": 64,
                "overflow_policy": "explicit_episode_rejection",
            },
            "depth": {
                "requested": config.capture_depth,
                "complete": (not config.capture_depth or bool(result.wrist_depth)),
            },
        }
        return writer.finalize(
            episode=episode,
            samples=rows,
            sync_report=sync_report,
            timestamp_quality=timestamp_quality,
            calibration=config.calibration,
        )
    except BaseException as exc:
        if not writer.finished:
            writer.abort("collector_failure", details={"error": str(exc)})
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(channel.close() for channel in channels))


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value
