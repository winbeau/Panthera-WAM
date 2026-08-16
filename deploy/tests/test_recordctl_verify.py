"""recordctl.sh verify 质量门回归测试（缺帧门限与 collectord ≤2% 对齐）。

背景：staging 对缺失相机帧复制上一帧（时间线无空洞），sync_report 里的
missing_frames 只是原始丢帧审计数；collectord 门限为 max(3, 2%·canonical)。
verify 必须用同一门限，否则每次 overhead 偶发丢帧都会误杀合法 episode。
duplicate/gaps/overflow/regression 仍是 0 容忍。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "recordctl.sh"

ZEROS = {"overhead_rgb": 0, "state": 0, "wrist_depth": 0, "wrist_rgb": 0}


def make_episode(root, *, missing=0, canonical=1431, duplicate=0, gaps=0, overflow=0, regressions=0):
    ep = root / "episodes" / "ep001"
    ep.mkdir(parents=True)
    (ep / "COMPLETE").write_text("ok\n")
    (ep / "episode.json").write_text(
        json.dumps(
            {
                "episode_id": "ep001",
                "success": True,
                "fixed_length": {"enabled": False},
                "depth": {"complete": True, "requested": True},
                "motion_scope": "task_action_only",
            }
        )
    )
    (ep / "sync_report.json").write_text(
        json.dumps(
            {
                "valid_ticks": canonical,
                "canonical_ticks": canonical,
                "missing_frames": {**ZEROS, "overhead_rgb": missing},
                "duplicate_frames": {**ZEROS, "overhead_rgb": duplicate},
                "sequence_gaps": {**ZEROS, "overhead_rgb": gaps},
                "ring_overflows": {**ZEROS, "overhead_rgb": overflow},
                "timestamp_regressions": regressions,
            }
        )
    )


def run_verify(tmp_path, **kw):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "deploy" / "recordctl.sh")
    python = repo / ".venv" / "bin" / "python"
    python.symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "panthera-data"
    root.mkdir()
    make_episode(root, **kw)
    env = {
        **os.environ,
        "HOME": str(home),
        "PANTHERA_COLLECTION_ROOT": str(root),
        "PANTHERA_RECORDCTL_STATE_DIR": str(tmp_path / "state"),
    }
    return subprocess.run(
        ["bash", str(repo / "deploy" / "recordctl.sh"), "verify", "ep001"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_missing_within_tolerance_passes(tmp_path):
    proc = run_verify(tmp_path, missing=5, canonical=1431)  # 0.35% ≤ max(3, 2%)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VERIFY_OK" in proc.stdout


def test_missing_beyond_tolerance_fails(tmp_path):
    proc = run_verify(tmp_path, missing=30, canonical=1431)  # > max(3, 29)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "quality gate failed: missing_frames" in proc.stderr


def test_small_episode_floor_of_three(tmp_path):
    # canonical=100：2% 得 2，底线 3 → 3 过、4 不过
    assert run_verify(tmp_path / "a", missing=3, canonical=100).returncode == 0
    assert run_verify(tmp_path / "b", missing=4, canonical=100).returncode == 1


def test_duplicate_still_zero_tolerance(tmp_path):
    proc = run_verify(tmp_path, duplicate=1)
    assert proc.returncode == 1
    assert "quality gate failed: duplicate_frames" in proc.stderr


def test_timestamp_regression_fails(tmp_path):
    proc = run_verify(tmp_path, regressions=1)
    assert proc.returncode == 1
    assert "timestamp_regressions" in proc.stderr
