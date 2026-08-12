"""Frozen P3 quality accounting and rejection gates."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

import numpy as np

from .schema import AlignedSample, CameraSample, StateSample


def _sequence_gaps(records: Sequence[StateSample | CameraSample]) -> int:
    gaps = 0
    for left, right in zip(records, records[1:]):
        if left.stream_instance_id != right.stream_instance_id:
            gaps += 1
        elif right.sequence != left.sequence + 1:
            gaps += abs(right.sequence - left.sequence - 1) or 1
    return gaps


def _timestamp_regressions(records: Sequence[StateSample | CameraSample]) -> int:
    if not records:
        return 0
    if isinstance(records[0], StateSample):
        values = [record.sampled_monotonic_ns for record in records]
    else:
        values = [record.alignment_monotonic_ns for record in records]
    return sum(right <= left for left, right in zip(values, values[1:]))


def _overflow_delta(records: Sequence[StateSample | CameraSample]) -> int:
    if not records:
        return 0
    # The service's lifetime overwrite counter rises whenever its bounded
    # retention window advances, even when this reader has consumed every
    # sequence. Reader loss is identified by an explicit sequence gap or by
    # the oldest retained sample overtaking the sample delivered to this
    # reader. This applies to both the 200 Hz state tap and camera rings.
    if isinstance(records[0], StateSample):
        return sum(record.tap_oldest_available_sequence > record.sequence for record in records)
    return sum(record.ring_oldest_available_sequence > record.sequence for record in records)


def _offset_summary(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"p50": None, "p95": None, "max_abs": None}
    array = np.asarray(values, dtype=np.int64)
    return {
        "p50": round(float(np.percentile(array, 50))),
        "p95": round(float(np.percentile(array, 95))),
        "max_abs": int(np.abs(array).max()),
    }


def build_sync_report(
    *,
    states: Sequence[StateSample],
    overhead_rgb: Sequence[CameraSample],
    wrist_rgb: Sequence[CameraSample],
    aligned: Sequence[AlignedSample],
    wrist_depth: Sequence[CameraSample] = (),
    disk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources: dict[str, Sequence[StateSample | CameraSample]] = {
        "state": states,
        "overhead_rgb": overhead_rgb,
        "wrist_rgb": wrist_rgb,
    }
    if wrist_depth:
        sources["wrist_depth"] = wrist_depth

    missing = Counter()
    for sample in aligned:
        for reason in sample.sync_reasons:
            if reason.startswith("missing_"):
                missing[reason.removeprefix("missing_")] += 1

    selected_sequences = {
        "overhead_rgb": [sample.overhead_rgb.sequence for sample in aligned if sample.overhead_rgb],
        "wrist_rgb": [sample.wrist_rgb.sequence for sample in aligned if sample.wrist_rgb],
        "wrist_depth": [sample.wrist_depth.sequence for sample in aligned if sample.wrist_depth],
    }
    duplicates = {
        key: len(values) - len(set(values))
        for key, values in selected_sequences.items()
        if key != "wrist_depth" or wrist_depth
    }
    state_offsets = [
        sample.state.sampled_monotonic_ns - sample.tick_monotonic_ns
        for sample in aligned
        if sample.state is not None
    ]
    overhead_offsets = [
        sample.overhead_rgb.alignment_monotonic_ns - sample.tick_monotonic_ns
        for sample in aligned
        if sample.overhead_rgb is not None
    ]
    wrist_offsets = [
        sample.wrist_rgb.alignment_monotonic_ns - sample.tick_monotonic_ns
        for sample in aligned
        if sample.wrist_rgb is not None
    ]
    report = {
        "canonical_ticks": len(aligned),
        "valid_ticks": sum(sample.sync_ok for sample in aligned),
        "received": {key: len(records) for key, records in sources.items()},
        "missing_frames": {key: int(missing.get(key, 0)) for key in sources},
        "duplicate_frames": duplicates,
        "sequence_gaps": {key: _sequence_gaps(records) for key, records in sources.items()},
        "ring_overflows": {key: _overflow_delta(records) for key, records in sources.items()},
        "timestamp_regressions": sum(_timestamp_regressions(records) for records in sources.values()),
        "state_interpolation_ratio": (
            sum(bool(sample.state and sample.state.interpolated) for sample in aligned) / len(aligned)
            if aligned
            else 0.0
        ),
        "offset_ns": {
            "state": _offset_summary(state_offsets),
            "overhead_rgb": _offset_summary(overhead_offsets),
            "wrist_rgb": _offset_summary(wrist_offsets),
        },
        "disabled_candidate_gates": {
            "valid_tick_ratio_min": 0.99,
            "state_p95_ns": 5_000_000,
            "state_max_abs_ns": 10_000_000,
            "camera_p95_ns": 25_000_000,
            "camera_max_abs_ns": 50_000_000,
        },
        "disk": disk or {},
    }
    return report


def build_timestamp_quality(
    *,
    states: Sequence[StateSample],
    overhead_rgb: Sequence[CameraSample],
    wrist_rgb: Sequence[CameraSample],
    wrist_depth: Sequence[CameraSample] = (),
    affine_fits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    camera_sources = {
        "overhead_rgb": overhead_rgb,
        "wrist_rgb": wrist_rgb,
    }
    if wrist_depth:
        camera_sources["wrist_depth"] = wrist_depth
    records: dict[str, Any] = {
        "state": {
            "source": "hardware_loop_state_tap",
            "quality": "pi_monotonic_sampled",
            "coverage_fraction": 1.0 if states else 0.0,
        }
    }
    total = len(states)
    covered = len(states)
    for name, frames in camera_sources.items():
        frame_count = len(frames)
        complete = sum(bool(frame.timestamp_source and frame.timestamp_quality) for frame in frames)
        records[name] = {
            "source": frames[0].timestamp_source if frames else "",
            "quality": frames[0].timestamp_quality if frames else "",
            "coverage_fraction": complete / frame_count if frame_count else 0.0,
            "host_observed_count": sum(
                frame.timestamp_quality in {"host_observed", "simulated"} for frame in frames
            ),
        }
        if affine_fits and name in affine_fits:
            fit = affine_fits[name]
            records[name]["affine_fit"] = fit.as_dict() if hasattr(fit, "as_dict") else fit
        total += frame_count
        covered += complete
    return {
        "coverage_fraction": covered / total if total else 0.0,
        "timestamp_regressions": sum(
            _timestamp_regressions(records_for_source)
            for records_for_source in (states, *camera_sources.values())
        ),
        **records,
    }


def quality_gate_reasons(
    sync_report: dict[str, Any],
    timestamp_quality: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if int(sync_report.get("canonical_ticks", 0)) < 2:
        reasons.append("fewer_than_two_canonical_ticks")
    if int(sync_report.get("valid_ticks", -1)) != int(sync_report.get("canonical_ticks", 0)):
        reasons.append("invalid_canonical_ticks")
    if int(sync_report.get("timestamp_regressions", -1)) != 0:
        reasons.append("timestamp_regression")
    for field in ("missing_frames", "duplicate_frames", "sequence_gaps", "ring_overflows"):
        values = sync_report.get(field)
        if not isinstance(values, dict) or any(int(value) != 0 for value in values.values()):
            reasons.append(field)
    if float(timestamp_quality.get("coverage_fraction", 0.0)) != 1.0:
        reasons.append("timestamp_metadata_coverage")
    if int(timestamp_quality.get("timestamp_regressions", -1)) != 0:
        reasons.append("timestamp_quality_regression")
    for key, record in timestamp_quality.items():
        if key in {"coverage_fraction", "timestamp_regressions"}:
            continue
        if not isinstance(record, dict):
            reasons.append(f"timestamp_quality_{key}")
            continue
        if not record.get("source") or not record.get("quality"):
            reasons.append(f"timestamp_identity_{key}")
        if float(record.get("coverage_fraction", 0.0)) != 1.0:
            reasons.append(f"timestamp_coverage_{key}")
    return reasons
