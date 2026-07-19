from __future__ import annotations

import json
import subprocess
import sys


def test_camerad_sim_check() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "panthera_camera",
            "--mode",
            "sim",
            "--width",
            "8",
            "--height",
            "6",
            "--check",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    data = json.loads(result.stdout)
    assert data == {
        "available": True,
        "model": "RealSense D405 Simulator",
        "width": 8,
        "height": 6,
        "bytes": 96,
    }
