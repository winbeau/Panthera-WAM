"""Same-filesystem atomic staging writer with explicit quality rejection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from .quality import quality_gate_reasons
from .schema import ACTION_SEMANTICS, AXES, FPS, SCHEMA_VERSION

APPROVAL_MARKER = ".panthera-usb3-ssd.json"
# 允许的采集根设备类别：usb3_ssd（外置盘）与 system_disk（系统盘，数据上传 HF 后清理）
APPROVED_DEVICE_CLASSES = frozenset({"usb3_ssd", "system_disk"})
_REQUIRED_IDENTITY_FIELDS = (
    "dataset_id",
    "task_id",
    "calibration_version",
    "camera_mount_version",
    "roi_version",
    "action_units_version",
)
_REQUIRED_CAMERA_FIELDS = (
    "path",
    "sequence",
    "device_timestamp_unit",
    "device_clock_domain",
    "host_receive_monotonic_ns",
    "host_publish_monotonic_ns",
    "estimated_capture_monotonic_ns",
    "timestamp_source",
    "timestamp_quality",
    "delta_ns",
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fsync_path(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finite_vector7(value: Any, *, field: str) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != len(AXES):
        raise ValueError(f"{field} must contain exactly {len(AXES)} values")
    if not all(math.isfinite(float(item)) for item in value):
        raise ValueError(f"{field} contains NaN or Inf")


def validate_staging_contents(
    staging: Path,
    *,
    episode: dict[str, Any],
    samples: list[dict[str, Any]],
    sync_report: dict[str, Any],
    timestamp_quality: dict[str, Any],
) -> None:
    """Validate all producer-facing staging content before publishing COMPLETE."""
    if episode.get("fps") != FPS:
        raise ValueError(f"episode fps must be {FPS}")
    if episode.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"episode schema_version must be {SCHEMA_VERSION!r}")
    if episode.get("action_semantics") != ACTION_SEMANTICS:
        raise ValueError(f"episode action_semantics must be {ACTION_SEMANTICS!r}")
    if episode.get("action_source") != "next_state_pseudo_action":
        raise ValueError("episode action_source must be 'next_state_pseudo_action'")
    commit = str(episode.get("panthera_wam_commit", ""))
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("episode panthera_wam_commit must be a lowercase 40-character Git SHA")
    identity = episode.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("episode identity must be an object")
    missing_identity = [field for field in _REQUIRED_IDENTITY_FIELDS if not identity.get(field)]
    if missing_identity:
        raise ValueError(f"episode identity is missing {missing_identity}")
    if int(sync_report.get("canonical_ticks", -1)) != len(samples):
        raise ValueError("sync_report canonical_ticks does not match samples")
    if int(sync_report.get("valid_ticks", -1)) != len(samples):
        raise ValueError("sync_report valid_ticks does not match samples")
    fixed_length = episode.get("fixed_length")
    if isinstance(fixed_length, dict) and bool(fixed_length.get("enabled")):
        expected_ticks = int(fixed_length.get("canonical_ticks", -1))
        if expected_ticks < 2 or len(samples) != expected_ticks:
            raise ValueError(
                "fixed-length episode tick count does not match fixed_length.canonical_ticks"
            )
    if not math.isclose(float(timestamp_quality.get("coverage_fraction", 0.0)), 1.0):
        raise ValueError("timestamp coverage_fraction must be 1.0")

    depth = episode.get("depth")
    depth_required = isinstance(depth, dict) and bool(depth.get("requested"))
    if depth_required and not depth.get("complete"):
        raise ValueError("requested depth stream is incomplete")
    camera_keys = ["overhead_rgb", "wrist_rgb"] + (["wrist_depth"] if depth_required else [])
    previous_tick = -1
    previous_state_sequence = -1
    previous_state_timestamp = -1
    previous_camera: dict[str, tuple[int, int, int, float | None, int | None, str, str]] = {}
    image_shapes: dict[str, tuple[int, int]] = {}
    for index, sample in enumerate(samples):
        tick = int(sample.get("tick_monotonic_ns", 0))
        if tick <= previous_tick:
            raise ValueError(f"sample[{index}] tick_monotonic_ns is not strictly increasing")
        previous_tick = tick
        state = sample.get("state")
        if not isinstance(state, dict):
            raise ValueError(f"sample[{index}].state must be an object")
        sequence = int(state.get("sequence", 0))
        sampled_ns = int(state.get("sampled_monotonic_ns", 0))
        if sequence <= previous_state_sequence or sampled_ns <= previous_state_timestamp:
            raise ValueError(f"sample[{index}] state sequence/timestamp is not strictly increasing")
        previous_state_sequence = sequence
        previous_state_timestamp = sampled_ns
        _finite_vector7(state.get("position"), field=f"sample[{index}].state.position")
        _finite_vector7(state.get("velocity"), field=f"sample[{index}].state.velocity")
        if sample.get("sync_ok") is not True or sample.get("sync_reasons") != []:
            raise ValueError(f"sample[{index}] failed synchronization gates")

        for camera_key in camera_keys:
            camera = sample.get(camera_key)
            if not isinstance(camera, dict):
                raise ValueError(f"sample[{index}].{camera_key} must be an object")
            missing = [field for field in _REQUIRED_CAMERA_FIELDS if field not in camera]
            if missing:
                raise ValueError(f"sample[{index}].{camera_key} is missing {missing}")
            camera_sequence = int(camera["sequence"])
            receive_ns = int(camera["host_receive_monotonic_ns"])
            publish_ns = int(camera["host_publish_monotonic_ns"])
            if camera_sequence <= 0 or receive_ns <= 0 or publish_ns < receive_ns:
                raise ValueError(f"sample[{index}].{camera_key} has invalid sequence/timestamps")
            estimated_raw = camera.get("estimated_capture_monotonic_ns")
            estimated_ns = int(estimated_raw) if estimated_raw is not None else None
            if estimated_ns is not None and int(camera["delta_ns"]) != estimated_ns - tick:
                raise ValueError(f"sample[{index}].{camera_key} delta_ns mismatch")
            raw = camera.get("device_timestamp_raw")
            raw_timestamp = float(raw) if raw is not None else None
            clock_domain = str(camera["device_clock_domain"])
            stream_instance_id = str(camera.get("stream_instance_id", ""))
            previous = previous_camera.get(camera_key)
            if previous is not None:
                (
                    previous_sequence,
                    previous_receive,
                    previous_publish,
                    previous_raw,
                    previous_estimated,
                    previous_domain,
                    previous_instance,
                ) = previous
                if (
                    camera_sequence <= previous_sequence
                    or receive_ns <= previous_receive
                    or publish_ns <= previous_publish
                ):
                    raise ValueError(f"sample[{index}].{camera_key} sequence/timestamp regressed")
                if raw_timestamp is not None and previous_raw is not None and raw_timestamp <= previous_raw:
                    raise ValueError(f"sample[{index}].{camera_key} device timestamp regressed")
                if (
                    estimated_ns is not None
                    and previous_estimated is not None
                    and estimated_ns <= previous_estimated
                ):
                    raise ValueError(f"sample[{index}].{camera_key} estimated timestamp regressed")
                if clock_domain != previous_domain or stream_instance_id != previous_instance:
                    raise ValueError(f"sample[{index}].{camera_key} source identity changed")
            previous_camera[camera_key] = (
                camera_sequence,
                receive_ns,
                publish_ns,
                raw_timestamp,
                estimated_ns,
                clock_domain,
                stream_instance_id,
            )
            image_path = (staging / str(camera["path"])).resolve()
            if not image_path.is_relative_to(staging) or not image_path.is_file():
                raise ValueError(f"sample[{index}].{camera_key} path is invalid")
            with Image.open(image_path) as image:
                image.verify()
            with Image.open(image_path) as image:
                if camera_key == "wrist_depth" and image.mode not in ("I;16", "I"):
                    raise ValueError(f"sample[{index}] depth image must be 16-bit")
                shape = (image.height, image.width)
            previous_shape = image_shapes.setdefault(camera_key, shape)
            if shape != previous_shape:
                raise ValueError(f"sample[{index}].{camera_key} shape changed within episode")


def validate_collection_root(root: Path, *, allow_unapproved: bool = False) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if allow_unapproved:
        return root
    marker_path = root / APPROVAL_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"collection root is not approved; missing valid {marker_path}") from exc
    if marker.get("approved") is not True or marker.get("device_class") not in APPROVED_DEVICE_CLASSES:
        raise ValueError(
            f"collection root marker must approve one of {sorted(APPROVED_DEVICE_CLASSES)}"
        )
    return root


class AtomicEpisodeWriter:
    def __init__(
        self,
        root: Path,
        episode_id: str,
        *,
        allow_unapproved_root: bool = False,
    ) -> None:
        if not episode_id or "/" in episode_id or "\\" in episode_id:
            raise ValueError("episode_id must be a single non-empty path component")
        self.root = validate_collection_root(root, allow_unapproved=allow_unapproved_root)
        self.episodes_dir = self.root / "episodes"
        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        self.final_path = self.episodes_dir / episode_id
        self.temporary_path = self.episodes_dir / f".{episode_id}.tmp-{uuid.uuid4().hex}"
        if self.final_path.exists():
            raise FileExistsError(self.final_path)
        self.temporary_path.mkdir()
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def path(self, relative: str) -> Path:
        path = (self.temporary_path / relative).resolve()
        if not path.is_relative_to(self.temporary_path):
            raise ValueError(f"staging path escapes episode: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def finalize(
        self,
        *,
        episode: dict[str, Any],
        samples: list[dict[str, Any]],
        sync_report: dict[str, Any],
        timestamp_quality: dict[str, Any],
        calibration: dict[str, Any],
    ) -> Path:
        if self._finished:
            raise RuntimeError("episode writer is already finished")
        reasons = quality_gate_reasons(sync_report, timestamp_quality)
        if reasons:
            self.abort("quality_gate_failure", details={"reasons": reasons})
            raise ValueError(f"episode failed quality gates: {reasons}")
        if len(samples) < 2:
            self.abort("insufficient_samples")
            raise ValueError("at least two aligned samples are required")

        calibration_path = self.path("calibration.json")
        _write_json(calibration_path, calibration)
        calibration_sha256 = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
        episode = dict(episode)
        episode["calibration_sha256"] = calibration_sha256
        _write_json(self.path("episode.json"), episode)
        pq.write_table(
            pa.Table.from_pylist(samples),
            self.path("samples.parquet"),
            compression="zstd",
        )
        _write_json(self.path("sync_report.json"), sync_report)
        _write_json(self.path("timestamp_quality.json"), timestamp_quality)
        try:
            validate_staging_contents(
                self.temporary_path,
                episode=episode,
                samples=samples,
                sync_report=sync_report,
                timestamp_quality=timestamp_quality,
            )
        except (OSError, TypeError, ValueError) as exc:
            self.abort("staging_validation_failure", details={"error": str(exc)})
            raise ValueError(f"episode failed staging validation: {exc}") from exc

        for path in sorted(item for item in self.temporary_path.rglob("*") if item.is_file()):
            _fsync_path(path)
        for path in sorted(
            (item for item in self.temporary_path.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _fsync_directory(path)
        complete = self.path("COMPLETE")
        complete.write_text("complete\n", encoding="utf-8")
        _fsync_path(complete)
        _fsync_directory(self.temporary_path)
        os.replace(self.temporary_path, self.final_path)
        _fsync_directory(self.episodes_dir)
        self._finished = True
        return self.final_path

    def abort(
        self,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> Path:
        if self._finished:
            raise RuntimeError("episode writer is already finished")
        _write_json(
            self.path("FAILED.json"),
            {"reason": reason, "details": details or {}},
        )
        _fsync_path(self.path("FAILED.json"))
        _fsync_directory(self.temporary_path)
        self._finished = True
        return self.temporary_path
