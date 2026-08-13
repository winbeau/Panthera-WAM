from __future__ import annotations

from pathlib import Path

import pytest

from armd.collectord.alignment import (
    align_episode,
    canonical_ticks,
    estimate_motion_offset,
    interpolate_states,
    select_nearest_unique,
)
from armd.collectord.schema import CameraSample, StateSample


def state(sequence: int, timestamp_ns: int, value: float) -> StateSample:
    return StateSample(
        sequence=sequence,
        sampled_monotonic_ns=timestamp_ns,
        position=(value,) * 7,
        velocity=(value * 2,) * 7,
        torque=(0.0,) * 7,
        valid=(True,) * 7,
        mode=(0,) * 7,
        fault=(0,) * 7,
        estop_engaged=False,
        stream_instance_id="state-epoch",
    )


def camera(sequence: int, timestamp_ns: int) -> CameraSample:
    return CameraSample(
        stream_name="overhead_rgb",
        sequence=sequence,
        stream_instance_id="camera-epoch",
        path=Path(f"{sequence}.jpg"),
        width=8,
        height=6,
        pixel_format="jpeg",
        device_timestamp_raw=None,
        device_timestamp_unit="unspecified",
        device_clock_domain="host_monotonic",
        host_receive_monotonic_ns=timestamp_ns,
        host_publish_monotonic_ns=timestamp_ns + 1,
        estimated_capture_monotonic_ns=None,
        timestamp_source="host_receive",
        timestamp_quality="host_observed",
    )


def test_canonical_ticks_do_not_accumulate_fractional_drift() -> None:
    start = 1_000_000_000
    ticks = canonical_ticks(start, start + 2_000_000_000)
    assert len(ticks) == 61
    assert ticks[30] - start == 1_000_000_000
    assert ticks[60] - start == 2_000_000_000
    intervals = {right - left for left, right in zip(ticks, ticks[1:])}
    assert intervals == {33_333_333, 33_333_334}


def test_fixed_alignment_publishes_exact_tick_count_without_padding() -> None:
    samples = [state(index + 1, 1_000_000_000 + index * 33_333_333, float(index)) for index in range(5)]
    cameras = [camera(index + 1, 1_000_000_000 + index * 33_333_333) for index in range(5)]
    aligned = align_episode(
        states=samples,
        overhead_rgb=cameras,
        wrist_rgb=[camera(index + 1, 1_000_000_000 + index * 33_333_333) for index in range(5)],
        fixed_ticks=3,
    )
    assert len(aligned) == 3
    assert [sample.tick_index for sample in aligned] == [0, 1, 2]
    assert aligned[-1].state is not None
    assert aligned[-1].state.position == pytest.approx((3.0,) * 7)


def test_fixed_alignment_rejects_short_common_window() -> None:
    samples = [state(1, 1_000_000_000, 0.0), state(2, 1_033_333_333, 1.0)]
    cameras = [camera(1, 1_000_000_000), camera(2, 1_033_333_333)]
    with pytest.raises(ValueError, match="fixed-length episode requires"):
        align_episode(
            states=samples,
            overhead_rgb=cameras,
            wrist_rgb=cameras,
            fixed_ticks=3,
        )


def test_state_interpolation_is_linear_and_bounded() -> None:
    samples = [state(1, 1_000, 0.0), state(2, 2_000, 1.0)]
    aligned = interpolate_states(samples, [1_000, 1_500, 2_000])
    assert aligned[0] is not None and not aligned[0].interpolated
    assert aligned[1] is not None and aligned[1].interpolated
    assert aligned[1].position == pytest.approx((0.5,) * 7)
    assert aligned[1].velocity == pytest.approx((1.0,) * 7)
    assert aligned[1].freshness_ns == 500
    assert aligned[2] is not None and not aligned[2].interpolated


def test_motion_offset_search_checks_minus_two_through_plus_two() -> None:
    state_motion = [0.0, 1.0, 4.0, 2.0, 5.0, 3.0, 1.0]
    camera_motion = [9.0, 9.0, *state_motion[:-2]]
    offset, scores = estimate_motion_offset(camera_motion, state_motion)
    assert set(scores) == {-2, -1, 0, 1, 2}
    assert offset == -2


def test_motion_offset_is_null_when_motion_is_insufficient() -> None:
    offset, scores = estimate_motion_offset([0.0] * 8, [0.0] * 8)
    assert offset is None
    assert all(score is None for score in scores.values())


def test_camera_selection_never_silently_reuses_a_frame() -> None:
    frames = [camera(1, 100), camera(2, 200)]
    selected = select_nearest_unique(
        frames,
        [90, 110, 210],
        timestamp=lambda frame: frame.alignment_monotonic_ns,
    )
    assert selected[0] is not None and selected[0].sequence == 1
    assert selected[1] is not None and selected[1].sequence == 2
    assert selected[2] is None
