from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "preview-record.py"
SPEC = importlib.util.spec_from_file_location("preview_record", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
preview_record = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview_record)


def test_variable_length_quality_uses_actual_duration() -> None:
    thresholds = preview_record.quality_thresholds(116.186649, 8.0)

    assert thresholds == {"state": 11618, "wrist": 697, "overhead": 697}
    assert preview_record.preview_succeeded(
        [],
        {"timestamp_regressions": 0},
        {"state": 15225, "wrist": 833, "overhead": 700},
        thresholds,
    )
    assert not preview_record.preview_succeeded(
        [],
        {"timestamp_regressions": 0},
        {"state": 15225, "wrist": 833, "overhead": 575},
        thresholds,
    )


def test_video_pts_preserves_wall_clock_gaps() -> None:
    start_ns = 10_000_000_000

    first = preview_record.video_pts(start_ns, start_ns, -1)
    second = preview_record.video_pts(start_ns + 125_000_000, start_ns, first)
    after_gap = preview_record.video_pts(start_ns + 1_000_000_000, start_ns, second)

    assert first == 0
    assert second == 11_250
    assert after_gap == 90_000
