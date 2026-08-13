from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from .schema import ACTION_SEMANTICS, FPS, SCHEMA_VERSION


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _camera_record(
    *,
    path: str,
    sequence: int,
    tick_ns: int,
    device_timestamp_raw: int | float | None,
    device_timestamp_unit: str,
    device_clock_domain: str,
    receive_delay_ns: int,
    publish_delay_ns: int,
    source: str,
    quality: str,
) -> dict[str, object]:
    estimated_capture_ns = tick_ns - 2_000_000
    return {
        "path": path,
        "sequence": sequence,
        "device_timestamp_raw": device_timestamp_raw,
        "device_timestamp_unit": device_timestamp_unit,
        "device_clock_domain": device_clock_domain,
        "host_receive_monotonic_ns": estimated_capture_ns + receive_delay_ns,
        "host_publish_monotonic_ns": estimated_capture_ns + publish_delay_ns,
        "estimated_capture_monotonic_ns": estimated_capture_ns,
        "timestamp_source": source,
        "timestamp_quality": quality,
        "delta_ns": estimated_capture_ns - tick_ns,
    }


def create_staging_fixture(output: Path, *, overwrite: bool = False) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        if not overwrite:
            raise FileExistsError(output)
        shutil.rmtree(output)

    (output / "overhead").mkdir(parents=True)
    (output / "wrist_rgb").mkdir()
    (output / "wrist_depth").mkdir()

    episode_id = "fixture-color-block-000001"
    calibration = {
        "version": "fixture-calibration-v1",
        "workspace_roi": [0, 0, 64, 64],
        "camera_mount_version": "fixture-camera-mount-v1",
        "synthetic": True,
    }
    _write_json(output / "calibration.json", calibration)
    calibration_sha256 = hashlib.sha256((output / "calibration.json").read_bytes()).hexdigest()
    episode = {
        "episode_id": episode_id,
        "canonical_task": "Move the red block from the start area to the target area.",
        "aliases_zh": ["把红色方块移到目标区"],
        "aliases_en": ["Move the red block to the target."],
        "operator": "synthetic-fixture",
        "success": True,
        "failure_reason": None,
        "fps": FPS,
        "action_semantics": ACTION_SEMANTICS,
        "action_source": "next_state_pseudo_action",
        "schema_version": SCHEMA_VERSION,
        "camera_state_offset_frames": 0,
        "offset_estimation_method": "synthetic_exact",
        "started_wall_time": "2026-01-01T00:00:00Z",
        "finished_wall_time": "2026-01-01T00:00:00.200000Z",
        "started_monotonic_ns": 1_000_000_000,
        "finished_monotonic_ns": 1_200_000_000,
        "panthera_wam_commit": "77cc8aad0b2e278bab6202d1fc61c79a1af18424",
        "calibration_sha256": calibration_sha256,
        "queue_policy": {
            "state_capacity": 4096,
            "camera_capacity": 64,
            "overflow_policy": "explicit_episode_rejection",
        },
        "depth": {"requested": True, "complete": True},
        "fixed_length": {"enabled": True, "canonical_ticks": 6, "duration_s": 5 / FPS},
        "identity": {
            "dataset_id": "panthera-color-block-fixture-v1",
            "task_id": "color_block_red_to_target_v1",
            "calibration_version": "fixture-calibration-v1",
            "camera_mount_version": "fixture-camera-mount-v1",
            "roi_version": "fixture-roi-v1",
            "action_units_version": "panthera-7axis-rad-nativegripper-v1",
        },
        "robot": {"model": "Panthera-HT", "serial": "fixture-robot"},
        "cameras": {
            "overhead": {"model": "C920e", "serial": "fixture-c920e"},
            "wrist": {"model": "D405", "serial": "fixture-d405"},
        },
    }
    _write_json(output / "episode.json", episode)

    base_tick_ns = 1_000_000_000
    step_ns = 1_000_000_000 // FPS
    samples: list[dict[str, object]] = []
    for index in range(6):
        tick_ns = base_tick_ns + index * step_ns
        overhead_name = f"overhead/{index:06d}.png"
        wrist_name = f"wrist_rgb/{index:06d}.png"
        depth_name = f"wrist_depth/{index:06d}.png"

        yy, xx = np.mgrid[0:64, 0:64]
        overhead = np.zeros((64, 64, 3), dtype=np.uint8)
        overhead[..., 0] = np.uint8(160 + index * 10)
        overhead[..., 1] = np.uint8(xx * 2)
        overhead[..., 2] = np.uint8(yy)
        overhead[16:32, 8 + index : 24 + index, :] = np.array([255, 0, 0], dtype=np.uint8)

        wrist = np.zeros((64, 64, 3), dtype=np.uint8)
        wrist[..., 0] = np.uint8(yy)
        wrist[..., 1] = np.uint8(80 + index * 12)
        wrist[..., 2] = np.uint8(180 + index * 8)
        wrist[24:40, 32 - index : 48 - index, :] = np.array([0, 255, 255], dtype=np.uint8)

        depth = (500 + xx * 2 + yy + index * 5).astype(np.uint16)
        Image.fromarray(overhead, mode="RGB").save(output / overhead_name, format="PNG")
        Image.fromarray(wrist, mode="RGB").save(output / wrist_name, format="PNG")
        Image.fromarray(depth).save(output / depth_name, format="PNG")

        position = [
            0.01 * index,
            0.20 + 0.005 * index,
            0.30 - 0.004 * index,
            -0.10 + 0.003 * index,
            0.05,
            -0.02,
            0.40 + 0.02 * index,
        ]
        velocity = [0.30, 0.15, -0.12, 0.09, 0.0, 0.0, 0.60]
        overhead_record = _camera_record(
            path=overhead_name,
            sequence=1_000 + index,
            tick_ns=tick_ns,
            device_timestamp_raw=tick_ns - 3_000_000,
            device_timestamp_unit="ns",
            device_clock_domain="v4l2_monotonic",
            receive_delay_ns=1_000_000,
            publish_delay_ns=1_500_000,
            source="v4l2_buffer_timestamp_mapped_to_pi_monotonic",
            quality="capture_estimated",
        )
        wrist_record = _camera_record(
            path=wrist_name,
            sequence=2_000 + index,
            tick_ns=tick_ns,
            device_timestamp_raw=index * (1000.0 / FPS),
            device_timestamp_unit="ms",
            device_clock_domain="realsense_hardware_clock",
            receive_delay_ns=1_500_000,
            publish_delay_ns=2_000_000,
            source="realsense_device_clock_affine_mapped_to_pi_monotonic",
            quality="device_clock_mapped",
        )
        samples.append(
            {
                "tick_index": index,
                "tick_monotonic_ns": tick_ns,
                "state": {
                    "position": position,
                    "velocity": velocity,
                    "sequence": 10_000 + index,
                    "sampled_monotonic_ns": tick_ns - 1_000_000,
                    "interpolated": index % 2 == 1,
                    "freshness_ns": 1_000_000,
                },
                "overhead_rgb": overhead_record,
                "wrist_rgb": wrist_record,
                "wrist_depth": {
                    **wrist_record,
                    "path": depth_name,
                    "depth_scale": 0.001,
                    "pixel_format": "Z16",
                },
                "sync_ok": True,
                "sync_reasons": [],
            }
        )

    pq.write_table(
        pa.Table.from_pylist(samples),
        output / "samples.parquet",
        compression="zstd",
    )
    _write_json(
        output / "sync_report.json",
        {
            "canonical_ticks": len(samples),
            "valid_ticks": len(samples),
            "missing_frames": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0, "wrist_depth": 0},
            "duplicate_frames": {"overhead_rgb": 0, "wrist_rgb": 0, "wrist_depth": 0},
            "sequence_gaps": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0, "wrist_depth": 0},
            "ring_overflows": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0, "wrist_depth": 0},
            "state_interpolation_ratio": 0.5,
            "timestamp_regressions": 0,
            "offset_ns": {
                "state": {"p50": -1_000_000, "p95": -1_000_000, "max_abs": 1_000_000},
                "overhead_rgb": {"p50": -2_000_000, "p95": -2_000_000, "max_abs": 2_000_000},
                "wrist_rgb": {"p50": -2_000_000, "p95": -2_000_000, "max_abs": 2_000_000},
            },
            "disk": {"bytes_written": 0, "duration_s": 0.2, "throughput_bytes_s": 0},
        },
    )
    _write_json(
        output / "timestamp_quality.json",
        {
            "coverage_fraction": 1.0,
            "timestamp_regressions": 0,
            "state": {
                "source": "hardware_loop_state_tap",
                "quality": "pi_monotonic_sampled",
                "coverage_fraction": 1.0,
            },
            "overhead_rgb": {
                "source": "v4l2_buffer_timestamp_mapped_to_pi_monotonic",
                "quality": "capture_estimated",
                "coverage_fraction": 1.0,
            },
            "wrist_rgb": {
                "source": "realsense_device_clock_affine_mapped_to_pi_monotonic",
                "quality": "device_clock_mapped",
                "coverage_fraction": 1.0,
                "affine_fit": {"slope": 1.0, "drift_ppm": 0.0, "residual_p95_ns": 0},
            },
            "wrist_depth": {
                "source": "realsense_device_clock_affine_mapped_to_pi_monotonic",
                "quality": "device_clock_mapped",
                "coverage_fraction": 1.0,
                "affine_fit": {"slope": 1.0, "drift_ppm": 0.0, "residual_p95_ns": 0},
            },
        },
    )
    (output / "COMPLETE").write_text("complete\n", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the deterministic Panthera v3 staging fixture")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    path = create_staging_fixture(args.output, overwrite=args.overwrite)
    print(path)


if __name__ == "__main__":
    main()
