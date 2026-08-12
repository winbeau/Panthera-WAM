"""Deterministic 30 Hz alignment without silent camera-frame reuse."""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from typing import TypeVar

import numpy as np
from PIL import Image

from .schema import AlignedSample, CameraSample, FPS, StateSample

RecordT = TypeVar("RecordT")


def canonical_ticks(start_ns: int, end_ns: int, *, fps: int = FPS) -> list[int]:
    if fps <= 0:
        raise ValueError("fps must be positive")
    if start_ns <= 0 or end_ns < start_ns:
        raise ValueError("invalid canonical tick bounds")
    duration_ns = end_ns - start_ns
    count = duration_ns * fps // 1_000_000_000 + 1
    return [start_ns + (index * 1_000_000_000 + fps // 2) // fps for index in range(count)]


def select_nearest_unique(
    records: Sequence[RecordT],
    ticks: Sequence[int],
    *,
    timestamp: Callable[[RecordT], int],
) -> list[RecordT | None]:
    ordered = sorted(records, key=timestamp)
    times = [timestamp(record) for record in ordered]
    used: set[int] = set()
    selected: list[RecordT | None] = []
    for tick in ticks:
        insertion = bisect.bisect_left(times, tick)
        candidates = []
        for index in (insertion - 1, insertion):
            if 0 <= index < len(ordered) and index not in used:
                candidates.append(index)
        if not candidates:
            selected.append(None)
            continue
        best = min(candidates, key=lambda index: (abs(times[index] - tick), times[index], index))
        used.add(best)
        selected.append(ordered[best])
    return selected


def interpolate_states(states: Sequence[StateSample], ticks: Sequence[int]) -> list[StateSample | None]:
    ordered = sorted(states, key=lambda state: state.sampled_monotonic_ns)
    times = [state.sampled_monotonic_ns for state in ordered]
    output: list[StateSample | None] = []
    for tick in ticks:
        insertion = bisect.bisect_left(times, tick)
        if insertion < len(ordered) and times[insertion] == tick:
            sample = ordered[insertion]
            output.append(
                sample.at_tick(
                    tick_monotonic_ns=tick,
                    position=sample.position,
                    velocity=sample.velocity,
                    interpolated=False,
                    freshness_ns=0,
                )
            )
            continue
        if insertion == 0 or insertion >= len(ordered):
            output.append(None)
            continue
        left = ordered[insertion - 1]
        right = ordered[insertion]
        span = right.sampled_monotonic_ns - left.sampled_monotonic_ns
        if span <= 0 or left.stream_instance_id != right.stream_instance_id:
            output.append(None)
            continue
        alpha = (tick - left.sampled_monotonic_ns) / span
        position = tuple(
            left_value + alpha * (right_value - left_value)
            for left_value, right_value in zip(left.position, right.position, strict=True)
        )
        velocity = tuple(
            left_value + alpha * (right_value - left_value)
            for left_value, right_value in zip(left.velocity, right.velocity, strict=True)
        )
        nearest = left if alpha <= 0.5 else right
        output.append(
            nearest.at_tick(
                tick_monotonic_ns=tick,
                position=position,
                velocity=velocity,
                interpolated=True,
                freshness_ns=max(tick - left.sampled_monotonic_ns, right.sampled_monotonic_ns - tick),
            )
        )
    return output


def estimate_motion_offset(
    camera_motion: Sequence[float],
    state_motion: Sequence[float],
    *,
    offsets: Sequence[int] = (-2, -1, 0, 1, 2),
) -> tuple[int | None, dict[int, float | None]]:
    camera = np.asarray(camera_motion, dtype=np.float64)
    state = np.asarray(state_motion, dtype=np.float64)
    if camera.shape != state.shape or camera.ndim != 1:
        raise ValueError("camera and state motion must be same-length 1D sequences")
    scores: dict[int, float | None] = {}
    for offset in offsets:
        if offset < 0:
            camera_view = camera[-offset:]
            state_view = state[: len(camera_view)]
        elif offset > 0:
            camera_view = camera[:-offset]
            state_view = state[offset:]
        else:
            camera_view = camera
            state_view = state
        if len(camera_view) < 3 or float(camera_view.std()) < 1e-9 or float(state_view.std()) < 1e-9:
            scores[int(offset)] = None
            continue
        scores[int(offset)] = float(np.corrcoef(camera_view, state_view)[0, 1])
    valid = {offset: score for offset, score in scores.items() if score is not None}
    if not valid:
        return None, scores
    return max(valid, key=lambda offset: (valid[offset], -abs(offset))), scores


def estimate_aligned_camera_state_offset(
    aligned: Sequence[AlignedSample],
) -> tuple[int | None, dict[int, float | None]]:
    if len(aligned) < 5 or any(sample.state is None or sample.overhead_rgb is None for sample in aligned):
        return None, {offset: None for offset in (-2, -1, 0, 1, 2)}
    state_motion = [
        float(np.linalg.norm(np.asarray(right.state.position) - np.asarray(left.state.position)))
        for left, right in zip(aligned, aligned[1:])
    ]
    images = []
    for sample in aligned:
        assert sample.overhead_rgb is not None
        with Image.open(sample.overhead_rgb.path) as image:
            images.append(np.asarray(image.convert("L"), dtype=np.float32))
    camera_motion = [float(np.mean(np.abs(right - left))) for left, right in zip(images, images[1:])]
    return estimate_motion_offset(camera_motion, state_motion)


def align_episode(
    *,
    states: Sequence[StateSample],
    overhead_rgb: Sequence[CameraSample],
    wrist_rgb: Sequence[CameraSample],
    wrist_depth: Sequence[CameraSample] = (),
    require_depth: bool = False,
) -> list[AlignedSample]:
    if not states or not overhead_rgb or not wrist_rgb:
        raise ValueError("state, overhead RGB, and wrist RGB streams are required")
    starts = [
        states[0].sampled_monotonic_ns,
        overhead_rgb[0].alignment_monotonic_ns,
        wrist_rgb[0].alignment_monotonic_ns,
    ]
    ends = [
        states[-1].sampled_monotonic_ns,
        overhead_rgb[-1].alignment_monotonic_ns,
        wrist_rgb[-1].alignment_monotonic_ns,
    ]
    if require_depth:
        if not wrist_depth:
            raise ValueError("depth was requested but no wrist depth frames were captured")
        starts.append(wrist_depth[0].alignment_monotonic_ns)
        ends.append(wrist_depth[-1].alignment_monotonic_ns)
    ticks = canonical_ticks(max(starts), min(ends))
    aligned_states = interpolate_states(states, ticks)
    aligned_overhead = select_nearest_unique(
        overhead_rgb,
        ticks,
        timestamp=lambda frame: frame.alignment_monotonic_ns,
    )
    aligned_wrist = select_nearest_unique(
        wrist_rgb,
        ticks,
        timestamp=lambda frame: frame.alignment_monotonic_ns,
    )
    aligned_depth = (
        select_nearest_unique(
            wrist_depth,
            ticks,
            timestamp=lambda frame: frame.alignment_monotonic_ns,
        )
        if wrist_depth
        else [None] * len(ticks)
    )

    output = []
    for index, tick in enumerate(ticks):
        reasons = []
        if aligned_states[index] is None:
            reasons.append("missing_state")
        if aligned_overhead[index] is None:
            reasons.append("missing_overhead_rgb")
        if aligned_wrist[index] is None:
            reasons.append("missing_wrist_rgb")
        if require_depth and aligned_depth[index] is None:
            reasons.append("missing_wrist_depth")
        output.append(
            AlignedSample(
                tick_index=index,
                tick_monotonic_ns=tick,
                state=aligned_states[index],
                overhead_rgb=aligned_overhead[index],
                wrist_rgb=aligned_wrist[index],
                wrist_depth=aligned_depth[index],
                sync_reasons=tuple(reasons),
            )
        )
    return output
