from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from armd.collectord.quality import build_sync_report, quality_gate_reasons
from armd.collectord.schema import AlignedSample, CameraSample, StateSample


def valid_sync_report() -> dict:
    return {
        "canonical_ticks": 3,
        "valid_ticks": 3,
        "timestamp_regressions": 0,
        "missing_frames": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0},
        "duplicate_frames": {"overhead_rgb": 0, "wrist_rgb": 0},
        "sequence_gaps": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0},
        "ring_overflows": {"state": 0, "overhead_rgb": 0, "wrist_rgb": 0},
    }


def valid_timestamp_quality() -> dict:
    source = {"source": "test", "quality": "test", "coverage_fraction": 1.0}
    return {
        "coverage_fraction": 1.0,
        "timestamp_regressions": 0,
        "state": dict(source),
        "overhead_rgb": dict(source),
        "wrist_rgb": dict(source),
    }


def test_first_post_baseline_overwrite_is_not_hidden() -> None:
    state = StateSample(
        sequence=1,
        sampled_monotonic_ns=1_000_000_000,
        position=(0.0,) * 7,
        velocity=(0.0,) * 7,
        torque=(0.0,) * 7,
        valid=(True,) * 7,
        mode=(0,) * 7,
        fault=(0,) * 7,
        estop_engaged=False,
        stream_instance_id="state",
        tap_overflow_count=1,
        tap_oldest_available_sequence=2,
    )

    def camera(name: str) -> CameraSample:
        return CameraSample(
            stream_name=name,
            sequence=1,
            stream_instance_id=name,
            path=Path(f"{name}.jpg"),
            width=2,
            height=2,
            pixel_format="jpeg",
            device_timestamp_raw=None,
            device_timestamp_unit="unspecified",
            device_clock_domain="unspecified",
            host_receive_monotonic_ns=1_000_000_000,
            host_publish_monotonic_ns=1_000_000_001,
            estimated_capture_monotonic_ns=None,
            timestamp_source="host_receive",
            timestamp_quality="host_observed",
        )

    overhead = camera("overhead_rgb")
    wrist = camera("wrist_rgb")
    states = [state, replace(state, sequence=2, sampled_monotonic_ns=1_005_000_000)]
    report = build_sync_report(
        states=states,
        overhead_rgb=[
            overhead,
            replace(
                overhead,
                sequence=2,
                host_receive_monotonic_ns=1_033_000_000,
                host_publish_monotonic_ns=1_033_000_001,
            ),
        ],
        wrist_rgb=[
            wrist,
            replace(
                wrist,
                sequence=2,
                host_receive_monotonic_ns=1_033_000_000,
                host_publish_monotonic_ns=1_033_000_001,
            ),
        ],
        aligned=[
            AlignedSample(0, 1_000_000_000, states[0], overhead, wrist),
            AlignedSample(
                1,
                1_033_000_000,
                states[1],
                replace(
                    overhead,
                    sequence=2,
                    host_receive_monotonic_ns=1_033_000_000,
                    host_publish_monotonic_ns=1_033_000_001,
                ),
                replace(
                    wrist,
                    sequence=2,
                    host_receive_monotonic_ns=1_033_000_000,
                    host_publish_monotonic_ns=1_033_000_001,
                ),
            ),
        ],
    )
    assert report["ring_overflows"]["state"] == 1


def test_state_lifetime_overwrite_counter_is_not_reader_loss() -> None:
    state = StateSample(
        sequence=5000,
        sampled_monotonic_ns=1_000_000_000,
        position=(0.0,) * 7,
        velocity=(0.0,) * 7,
        torque=(0.0,) * 7,
        valid=(True,) * 7,
        mode=(0,) * 7,
        fault=(0,) * 7,
        estop_engaged=False,
        stream_instance_id="state",
        tap_overflow_count=100,
        tap_oldest_available_sequence=1000,
    )
    report = build_sync_report(
        states=[
            state,
            replace(
                state,
                sequence=5001,
                sampled_monotonic_ns=1_005_000_000,
                tap_overflow_count=101,
                tap_oldest_available_sequence=1001,
            ),
        ],
        overhead_rgb=[],
        wrist_rgb=[],
        aligned=[],
    )
    assert report["sequence_gaps"]["state"] == 0
    assert report["ring_overflows"]["state"] == 0


def test_camera_lifetime_overwrite_counter_is_not_reader_loss() -> None:
    state = StateSample(
        sequence=1,
        sampled_monotonic_ns=1_000_000_000,
        position=(0.0,) * 7,
        velocity=(0.0,) * 7,
        torque=(0.0,) * 7,
        valid=(True,) * 7,
        mode=(0,) * 7,
        fault=(0,) * 7,
        estop_engaged=False,
        stream_instance_id="state",
    )
    frames = [
        CameraSample(
            stream_name="overhead_rgb",
            sequence=index,
            stream_instance_id="camera",
            path=Path(f"{index}.jpg"),
            width=2,
            height=2,
            pixel_format="jpeg",
            device_timestamp_raw=None,
            device_timestamp_unit="unspecified",
            device_clock_domain="unspecified",
            host_receive_monotonic_ns=1_000_000_000 + index,
            host_publish_monotonic_ns=1_000_000_100 + index,
            estimated_capture_monotonic_ns=None,
            timestamp_source="host_receive",
            timestamp_quality="host_observed",
            ring_overflow_count=index - 1,
            ring_oldest_available_sequence=max(1, index - 63),
        )
        for index in range(1, 101)
    ]
    report = build_sync_report(
        states=[state, replace(state, sequence=2, sampled_monotonic_ns=1_005_000_000)],
        overhead_rgb=frames,
        wrist_rgb=[replace(frame, stream_name="wrist_rgb", stream_instance_id="wrist") for frame in frames],
        aligned=[],
    )
    assert report["sequence_gaps"]["overhead_rgb"] == 0
    assert report["ring_overflows"]["overhead_rgb"] == 0


def test_quality_gates_accept_complete_accounting() -> None:
    assert quality_gate_reasons(valid_sync_report(), valid_timestamp_quality()) == []


def test_each_frozen_loss_gate_rejects_episode() -> None:
    for field in ("missing_frames", "duplicate_frames", "sequence_gaps", "ring_overflows"):
        report = valid_sync_report()
        report[field][next(iter(report[field]))] = 1
        assert field in quality_gate_reasons(report, valid_timestamp_quality())


def test_timestamp_metadata_must_have_full_coverage() -> None:
    quality = valid_timestamp_quality()
    quality["wrist_rgb"]["coverage_fraction"] = 0.99
    reasons = quality_gate_reasons(valid_sync_report(), quality)
    assert "timestamp_coverage_wrist_rgb" in reasons
