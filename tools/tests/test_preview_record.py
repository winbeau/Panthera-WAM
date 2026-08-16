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


def test_frame_index_pts_encoding_is_stable(tmp_path) -> None:
    """回归：preview MP4 用帧序 PTS。Pi 真机实测任何非均匀/墙钟 PTS
    （frame.time_base 或 stream.time_base=1/90000）都会在 ~第 17-22 帧 mux
    EINVAL（5/5 必现）；帧序 PTS 稳定。真实时间线在 preview.json 的
    camera_timing 与轨迹 jsonl，视频仅供人工回看。"""
    av = __import__("av")
    import numpy as np
    from fractions import Fraction

    frames = []
    for _ in range(20):
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        frames.append(av.VideoFrame.from_ndarray(image, format="rgb24"))

    output = tmp_path / "frame-index.mp4"
    container = av.open(str(output), mode="w")
    stream = container.add_stream("h264", rate=Fraction(8, 1))
    stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
    stream.options = {"preset": "ultrafast", "crf": "30"}
    try:
        for index, frame in enumerate(frames):
            frame.pts = index
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
        duration_s = reopened.duration / 1_000_000
    assert len(packets) == 20
    # 均匀帧距：容器时长 = 末帧 pts + 一帧时长 = 20/8 = 2.5s
    assert duration_s == pytest.approx(2.5, abs=0.05)
