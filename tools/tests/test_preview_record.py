from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


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


def test_stream_time_base_wall_clock_encoding(tmp_path) -> None:
    """回归：时间基必须设在 stream 上（frame 级 time_base 在 Pi 的
    PyAV/ffmpeg 62.3.1 上会 EINVAL 拒封，真实复现 5/5 必现）；同时验证
    编码后的 PTS 保留真实时间间隔（掉帧不压缩动作时间线）。"""
    av = __import__("av")
    import numpy as np
    from fractions import Fraction

    frames = []
    for index in range(20):
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        frames.append(av.VideoFrame.from_ndarray(image, format="rgb24"))
    # 第 0..9 帧每 0.125s 一帧，之后 1.5s 空档，再 0.125s 一帧
    start_ns = 10_000_000_000
    timestamps = [
        start_ns + int(i * 0.125 * 1_000_000_000) for i in range(10)
    ] + [
        start_ns + int((i - 10 + 12) * 0.125 * 1_000_000_000) + 1_500_000_000
        for i in range(10, 20)
    ]

    output = tmp_path / "wall-clock.mp4"
    container = av.open(str(output), mode="w")
    stream = container.add_stream("h264", rate=Fraction(8, 1))
    stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
    stream.time_base = preview_record.VIDEO_TIME_BASE
    stream.options = {"preset": "ultrafast", "crf": "30"}
    first_ns = timestamps[0]
    previous_pts = -1
    try:
        for frame, timestamp_ns in zip(frames, timestamps, strict=True):
            pts = preview_record.video_pts(timestamp_ns, first_ns, previous_pts)
            previous_pts = pts
            frame.pts = pts
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()

    with av.open(str(output)) as reopened:
        packets = [
            packet
            for packet in reopened.demux()
            if packet.stream.type == "video" and packet.pts is not None
        ]
    assert len(packets) == 20
    # 末帧时间戳 = 1.125s + 1.5s 空档 + 1.5s = 4.125s，空档必须保留在 PTS 里
    last_seconds = packets[-1].pts * packets[-1].time_base
    assert last_seconds == pytest.approx(4.125)
