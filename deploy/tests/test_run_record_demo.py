"""run-record-demo.sh 批量演示封装的黑盒回归。

等价链路：gozero → 每条（run-record 自动 rezero）→ 显式 rezero 幂等收尾。
复用 test_lerobot_collect_rezero 的假 panthera/recordctl。
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

from test_lerobot_collect_rezero import FAKE_PANTHERA, FAKE_RECORDCTL, SCRIPT

DEMO = Path(__file__).resolve().parents[2] / "deploy" / "run-record-demo.sh"


def build_fake_repo(tmp_path, flags, numbers):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    flags_dir = tmp_path / "flags"
    flags_dir.mkdir()
    for name in flags:
        (flags_dir / name).touch()
    calls = tmp_path / "calls.log"

    panthera = repo / ".venv" / "bin" / "panthera"
    panthera.write_text(FAKE_PANTHERA)
    panthera.chmod(panthera.stat().st_mode | stat.S_IXUSR)
    recordctl = repo / "deploy" / "recordctl.sh"
    recordctl.write_text(FAKE_RECORDCTL)
    recordctl.chmod(recordctl.stat().st_mode | stat.S_IXUSR)
    shutil.copy(SCRIPT, repo / "deploy" / "lerobot-collect.sh")
    shutil.copy(DEMO, repo / "deploy" / "run-record-demo.sh")

    # run-record 简写解析需要的 preview 轨迹布局
    for number in numbers:
        preview = home / "panthera-data" / "preview" / f"color-block_{number}"
        preview.mkdir(parents=True)
        (preview / f"replay_trajectory_{number}.jsonl").write_text('{"tick":0,"action":[0]*7}\n')
    return repo, home, flags_dir, calls


def run_demo(tmp_path, args, flags=(), numbers=("021", "022")):
    repo, home, flags_dir, calls = build_fake_repo(tmp_path, flags, numbers)
    env = {
        **os.environ,
        "HOME": str(home),
        "CALLS_LOG": str(calls),
        "FAKE_FLAGS": str(flags_dir),
    }
    proc = subprocess.run(
        ["bash", str(repo / "deploy" / "run-record-demo.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    call_lines = calls.read_text().splitlines() if calls.exists() else []
    return proc, call_lines


def test_demo_runs_gozero_then_run_record_rezero_sequence(tmp_path):
    proc, calls = run_demo(tmp_path, ["021", "022"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    gozero = [l for l in calls if l == "panthera workzero gozero --confirm --wait"]
    rezero = [l for l in calls if l == "panthera workzero rezero --confirm --wait"]
    play = [l for l in calls if l.startswith("panthera teach play")]
    assert len(gozero) == 1
    assert len(play) == 2
    # 每条 run-record 内部自动 rezero 一次 + 演示脚本显式 rezero 一次
    assert len(rezero) == 4
    assert "演示完成" in proc.stdout


def test_demo_fails_fast_on_first_run_record_failure(tmp_path):
    proc, calls = run_demo(tmp_path, ["021", "022"], flags=("play_fail",))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    play = [l for l in calls if l.startswith("panthera teach play")]
    assert len(play) == 1  # 第一条失败立即停止


def test_demo_rejects_bad_number(tmp_path):
    proc, calls = run_demo(tmp_path, ["21"])
    assert proc.returncode == 2
    assert "编号必须是三位数字" in proc.stderr
    assert not any("workzero gozero" in l for l in calls)
