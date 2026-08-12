from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from PIL import Image

from armd.collectord.schema import ACTION_SEMANTICS, SCHEMA_VERSION
from armd.collectord.staging import APPROVAL_MARKER, AtomicEpisodeWriter, validate_collection_root


def reports() -> tuple[dict, dict]:
    sync = {
        "canonical_ticks": 2,
        "valid_ticks": 2,
        "timestamp_regressions": 0,
        "missing_frames": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0},
        "duplicate_frames": {"overhead_rgb": 0, "wrist_rgb": 0},
        "sequence_gaps": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0},
        "ring_overflows": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0},
    }
    source = {"source": "test", "quality": "test", "coverage_fraction": 1.0}
    quality = {
        "coverage_fraction": 1.0,
        "timestamp_regressions": 0,
        "state": dict(source),
        "overhead_rgb": dict(source),
        "wrist_rgb": dict(source),
    }
    return sync, quality


def episode() -> dict:
    return {
        "episode_id": "episode-1",
        "fps": 30,
        "schema_version": SCHEMA_VERSION,
        "action_semantics": ACTION_SEMANTICS,
        "action_source": "next_state_pseudo_action",
        "panthera_wam_commit": "a" * 40,
        "identity": {
            "dataset_id": "test-dataset",
            "task_id": "test-task",
            "calibration_version": "cal-v1",
            "camera_mount_version": "mount-v1",
            "roi_version": "roi-v1",
            "action_units_version": "rad-v1",
        },
        "depth": {"requested": False, "complete": True},
    }


def samples(writer: AtomicEpisodeWriter) -> list[dict]:
    rows = []
    for index in range(2):
        tick = 1_000_000_000 + index * 33_333_333
        for stream in ("overhead", "wrist_rgb"):
            path = writer.path(f"{stream}/{index:06d}.png")
            Image.new("RGB", (8, 6), color=(index, 1, 2)).save(path)
        camera_base = {
            "sequence": index + 1,
            "device_timestamp_raw": float(index + 1),
            "device_timestamp_unit": "milliseconds",
            "device_clock_domain": "test-clock",
            "host_receive_monotonic_ns": tick + 1_000_000,
            "host_publish_monotonic_ns": tick + 2_000_000,
            "estimated_capture_monotonic_ns": tick,
            "timestamp_source": "device_to_host_estimate",
            "timestamp_quality": "estimated",
            "delta_ns": 0,
            "stream_instance_id": "camera-stream",
        }
        rows.append(
            {
                "tick_index": index,
                "tick_monotonic_ns": tick,
                "state": {
                    "position": [0.0] * 7,
                    "velocity": [0.0] * 7,
                    "sequence": index + 1,
                    "sampled_monotonic_ns": tick,
                },
                "overhead_rgb": {
                    **camera_base,
                    "path": f"overhead/{index:06d}.png",
                    "stream_instance_id": "overhead-stream",
                },
                "wrist_rgb": {
                    **camera_base,
                    "path": f"wrist_rgb/{index:06d}.png",
                    "stream_instance_id": "wrist-stream",
                },
                "sync_ok": True,
                "sync_reasons": [],
            }
        )
    return rows


def test_collection_root_requires_explicit_usb3_ssd_approval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not approved"):
        validate_collection_root(tmp_path)
    (tmp_path / APPROVAL_MARKER).write_text(
        json.dumps({"approved": True, "device_class": "usb3_ssd"}),
        encoding="utf-8",
    )
    assert validate_collection_root(tmp_path) == tmp_path.resolve()


def test_atomic_writer_publishes_only_after_full_validation(tmp_path: Path) -> None:
    writer = AtomicEpisodeWriter(tmp_path, "episode-1", allow_unapproved_root=True)
    sync, quality = reports()
    final = writer.finalize(
        episode=episode(),
        samples=samples(writer),
        sync_report=sync,
        timestamp_quality=quality,
        calibration={"version": "test"},
    )

    assert final == tmp_path.resolve() / "episodes/episode-1"
    assert (final / "COMPLETE").is_file()
    assert not (final / "FAILED.json").exists()
    assert pq.read_table(final / "samples.parquet").num_rows == 2
    observed_episode = json.loads((final / "episode.json").read_text(encoding="utf-8"))
    assert len(observed_episode["calibration_sha256"]) == 64
    assert (
        observed_episode["calibration_sha256"]
        == hashlib.sha256((final / "calibration.json").read_bytes()).hexdigest()
    )


def test_atomic_writer_never_marks_failed_quality_complete(tmp_path: Path) -> None:
    writer = AtomicEpisodeWriter(tmp_path, "episode-bad", allow_unapproved_root=True)
    sync, quality = reports()
    sync["sequence_gaps"]["state"] = 1
    with pytest.raises(ValueError, match="quality gates"):
        writer.finalize(
            episode=episode(),
            samples=samples(writer),
            sync_report=sync,
            timestamp_quality=quality,
            calibration={"version": "test"},
        )

    assert writer.temporary_path.is_dir()
    assert (writer.temporary_path / "FAILED.json").is_file()
    assert not (writer.temporary_path / "COMPLETE").exists()
    assert not writer.final_path.exists()


def test_atomic_writer_never_marks_malformed_content_complete(tmp_path: Path) -> None:
    writer = AtomicEpisodeWriter(tmp_path, "episode-malformed", allow_unapproved_root=True)
    sync, quality = reports()
    rows = samples(writer)
    rows[1]["wrist_rgb"]["path"] = "../escape.png"
    with pytest.raises(ValueError, match="staging validation"):
        writer.finalize(
            episode=episode(),
            samples=rows,
            sync_report=sync,
            timestamp_quality=quality,
            calibration={"version": "test"},
        )

    assert writer.temporary_path.is_dir()
    assert (writer.temporary_path / "FAILED.json").is_file()
    assert not (writer.temporary_path / "COMPLETE").exists()
    assert not writer.final_path.exists()
