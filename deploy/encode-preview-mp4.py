#!/usr/bin/env python
"""Encode preview camera frames (JPEG/PNG) into mp4 videos (wrist + overhead).

Usage:
    python deploy/encode-preview-mp4.py <frames-dir> <out-dir> <task> <num>

Frames must be named wrist_*.{jpg,png} / overhead_*.{jpg,png} (zero-padded
sequence order). Outputs <task>_wrist_<num>.mp4 and <task>_overhead_<num>.mp4
at 0.5 fps (2 s per frame, matching the preview capture cadence).
Requires PyAV and Pillow in the armd venv.

用完后配合 deploy/upload-hf.sh preview 上传到 HF：
    ./deploy/upload-hf.sh preview <out-dir> <task> <num>
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image

FRAME_INTERVAL_S = 2  # 抓帧间隔（0.5 fps 真实节奏）


def encode(frames: list[Path], out: Path, fps: Fraction) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    probe = av.VideoFrame.from_image(Image.open(frames[0]))
    with av.open(str(out), mode="w") as container:
        stream = container.add_stream("h264", rate=fps)
        stream.width = probe.width
        stream.height = probe.height
        stream.pix_fmt = "yuv420p"
        stream.options = {"preset": "medium", "crf": "23"}
        for p in frames:
            frame = av.VideoFrame.from_image(Image.open(p).convert("RGB"))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    print(f"{out.name}: {len(frames)} frames @ 1/{FRAME_INTERVAL_S} fps -> {out.stat().st_size / 1e6:.1f} MB", flush=True)


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    task = sys.argv[3]
    num = sys.argv[4]
    wrist = sorted(src.glob("wrist_*.jpg")) + sorted(src.glob("wrist_*.png"))
    overhead = sorted(src.glob("overhead_*.jpg")) + sorted(src.glob("overhead_*.png"))
    if not wrist or not overhead:
        raise SystemExit(f"frames missing: wrist={len(wrist)} overhead={len(overhead)}")
    encode(wrist, out_dir / f"{task}_wrist_{num}.mp4", Fraction(1, FRAME_INTERVAL_S))
    encode(overhead, out_dir / f"{task}_overhead_{num}.mp4", Fraction(1, FRAME_INTERVAL_S))


if __name__ == "__main__":
    main()
