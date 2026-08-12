"""Typed records shared by collectord alignment, quality, and staging."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

AXES = (
    "joint_1",
    "joint_2",
    "joint_3",
    "joint_4",
    "joint_5",
    "joint_6",
    "gripper",
)
FPS = 30
SCHEMA_VERSION = "panthera-fastwam-v1"
ACTION_SEMANTICS = "next_absolute_position_waypoint_q_t_plus_1_30hz"


def _vector7(values: tuple[float, ...], field: str) -> tuple[float, ...]:
    if len(values) != len(AXES):
        raise ValueError(f"{field} must contain exactly {len(AXES)} values")
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{field} contains NaN or Inf")
    return tuple(float(value) for value in values)


@dataclass(frozen=True, slots=True)
class StateSample:
    sequence: int
    sampled_monotonic_ns: int
    position: tuple[float, ...]
    velocity: tuple[float, ...]
    torque: tuple[float, ...]
    valid: tuple[bool, ...]
    mode: tuple[int, ...]
    fault: tuple[int, ...]
    estop_engaged: bool
    stream_instance_id: str
    tap_overflow_count: int = 0
    tap_oldest_available_sequence: int = 0
    interpolated: bool = False
    freshness_ns: int = 0

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.sampled_monotonic_ns <= 0:
            raise ValueError("state sequence and timestamp must be positive")
        object.__setattr__(self, "position", _vector7(self.position, "position"))
        object.__setattr__(self, "velocity", _vector7(self.velocity, "velocity"))
        object.__setattr__(self, "torque", _vector7(self.torque, "torque"))
        for field, values in (("valid", self.valid), ("mode", self.mode), ("fault", self.fault)):
            if len(values) != len(AXES):
                raise ValueError(f"{field} must contain exactly {len(AXES)} values")
        if not self.stream_instance_id:
            raise ValueError("state stream_instance_id is required")
        if self.tap_overflow_count < 0 or self.tap_oldest_available_sequence < 0 or self.freshness_ns < 0:
            raise ValueError("state counters cannot be negative")

    def at_tick(
        self,
        *,
        tick_monotonic_ns: int,
        position: tuple[float, ...],
        velocity: tuple[float, ...],
        interpolated: bool,
        freshness_ns: int,
    ) -> "StateSample":
        return replace(
            self,
            sampled_monotonic_ns=tick_monotonic_ns,
            position=position,
            velocity=velocity,
            interpolated=interpolated,
            freshness_ns=freshness_ns,
        )


@dataclass(frozen=True, slots=True)
class CameraSample:
    stream_name: str
    sequence: int
    stream_instance_id: str
    path: Path
    width: int
    height: int
    pixel_format: str
    device_timestamp_raw: float | None
    device_timestamp_unit: str
    device_clock_domain: str
    host_receive_monotonic_ns: int
    host_publish_monotonic_ns: int
    estimated_capture_monotonic_ns: int | None
    timestamp_source: str
    timestamp_quality: str
    device_frame_number: int = 0
    frameset_sequence: int = 0
    depth_scale: float = 0.0
    ring_overflow_count: int = 0
    ring_oldest_available_sequence: int = 0

    def __post_init__(self) -> None:
        if not self.stream_name or not self.stream_instance_id:
            raise ValueError("camera stream name and instance id are required")
        if self.sequence <= 0:
            raise ValueError("camera sequence must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("camera dimensions must be positive")
        if self.host_receive_monotonic_ns <= 0:
            raise ValueError("camera host receive timestamp must be positive")
        if self.host_publish_monotonic_ns < self.host_receive_monotonic_ns:
            raise ValueError("camera publish timestamp precedes receive timestamp")
        if not self.timestamp_source or not self.timestamp_quality:
            raise ValueError("camera timestamp source and quality are required")
        if self.device_timestamp_raw is not None and not math.isfinite(self.device_timestamp_raw):
            raise ValueError("camera device timestamp must be finite or null")
        if self.depth_scale < 0 or not math.isfinite(self.depth_scale):
            raise ValueError("depth scale must be finite and non-negative")
        if self.ring_overflow_count < 0 or self.ring_oldest_available_sequence < 0:
            raise ValueError("camera ring counters cannot be negative")

    @property
    def alignment_monotonic_ns(self) -> int:
        return self.estimated_capture_monotonic_ns or self.host_receive_monotonic_ns

    def staging_record(self, *, tick_monotonic_ns: int, relative_path: str) -> dict[str, Any]:
        return {
            "path": relative_path,
            "sequence": self.sequence,
            "device_timestamp_raw": self.device_timestamp_raw,
            "device_timestamp_unit": self.device_timestamp_unit,
            "device_clock_domain": self.device_clock_domain,
            "host_receive_monotonic_ns": self.host_receive_monotonic_ns,
            "host_publish_monotonic_ns": self.host_publish_monotonic_ns,
            "estimated_capture_monotonic_ns": self.estimated_capture_monotonic_ns,
            "timestamp_source": self.timestamp_source,
            "timestamp_quality": self.timestamp_quality,
            "delta_ns": self.alignment_monotonic_ns - tick_monotonic_ns,
            "device_frame_number": self.device_frame_number,
            "frameset_sequence": self.frameset_sequence,
            "stream_instance_id": self.stream_instance_id,
        }


@dataclass(frozen=True, slots=True)
class AlignedSample:
    tick_index: int
    tick_monotonic_ns: int
    state: StateSample | None
    overhead_rgb: CameraSample | None
    wrist_rgb: CameraSample | None
    wrist_depth: CameraSample | None = None
    sync_reasons: tuple[str, ...] = ()

    @property
    def sync_ok(self) -> bool:
        return not self.sync_reasons
