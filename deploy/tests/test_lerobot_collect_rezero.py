"""record-formal 自动 rezero 回归测试（deploy/lerobot-collect.sh，黑盒不碰真机）。

在临时目录搭一个 repo 副本，用假 panthera / 假 recordctl 驱动 record_formal()，
断言第 6-7 步语义：
  a. verify 成功     → 自动 rezero，退出 0；
  b. verify 失败     → 仍然自动 rezero，退出 1 且日志报出验收失败；
  c. COMPLETE 超时   → abort 残留 collectord + 自动 rezero，退出 1 且日志报出超时；
  d. rezero 自身失败 → 回退 formal_abort（恢复 teach 阻尼锁），退出 1。

真机语义（不在本测试范围内，由 armd/tests/test_workzero.py 覆盖）：
rezero = 开爪松方块 → 快速退出 teach → 定死锁+开爪窗口 → MoveL 回工作0位 → 定死锁。
"""

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "lerobot-collect.sh"

FAKE_PANTHERA = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    log() { printf 'panthera %s\\n' "$*" >> "$CALLS_LOG"; }
    log "$@"
    case "${1:-}" in
      control)
        case "${2:-}" in
          acquire|heartbeat) exit 0 ;;
          status) echo '{"estop_engaged": false, "held": true}' ;;
          *) echo "fake panthera: unknown control ${2:-}" >&2; exit 2 ;;
        esac ;;
      state)
        if [[ "${2:-}" == "get" && "${3:-}" == "--json" ]]; then
          echo '{"joints":[{"name":"joint1","valid":true,"fault":0,"mode":10},'
          echo '{"name":"joint2","valid":true,"fault":0,"mode":10},'
          echo '{"name":"joint3","valid":true,"fault":0,"mode":10},'
          echo '{"name":"joint4","valid":true,"fault":0,"mode":10},'
          echo '{"name":"joint5","valid":true,"fault":0,"mode":10},'
          echo '{"name":"joint6","valid":true,"fault":0,"mode":10}],'
          echo '"gripper":{"name":"gripper","valid":true,"fault":0,"mode":10}}'
        else
          echo "fake state table"
        fi ;;
      teach)
        case "${2:-}" in
          start) echo "teach started" ;;
          clutch) echo "state=hold ${4:-}" ;;
          play)
            if [[ -f "$FAKE_FLAGS/play_fail" ]]; then
              echo "teach play failed" >&2
              exit 1
            fi
            echo "play done" ;;
          *) echo "fake panthera: unknown teach ${2:-}" >&2; exit 2 ;;
        esac ;;
      workzero)
        if [[ "${2:-}" == "rezero" ]]; then
          if [[ -f "$FAKE_FLAGS/rezero_fail" ]]; then
            echo "rezero failed" >&2
            exit 1
          fi
          echo "rezero done"
        else
          echo "fake panthera: unknown workzero ${2:-}" >&2; exit 2
        fi ;;
      *)
        echo "fake panthera: unknown command ${1:-}" >&2
        exit 2 ;;
    esac
    exit 0
    """
)

FAKE_RECORDCTL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    log() { printf 'recordctl %s\\n' "$*" >> "$CALLS_LOG"; }
    log "$@"
    cmd="${1:-}"; ep="${2:-}"
    case "$cmd" in
      start) echo "==> collectord PID=99999" ;;
      status)
        n=0
        if [[ -f "$FAKE_FLAGS/status_calls" ]]; then n=$(cat "$FAKE_FLAGS/status_calls"); fi
        n=$((n + 1)); echo "$n" > "$FAKE_FLAGS/status_calls"
        echo "episode=$ep"
        if [[ -f "$FAKE_FLAGS/collectord_fail" && -f "$FAKE_FLAGS/stopped" ]]; then
          echo "process=NOT_RUNNING"
          echo "published=no"
          echo "failed=FAILED.json"
        else
          echo "process=RUNNING"
          if [[ -f "$FAKE_FLAGS/complete" ]]; then
            echo "published=COMPLETE"
          elif [[ -f "$FAKE_FLAGS/complete_late" && "$n" -ge 2 ]]; then
            echo "published=COMPLETE"
          else
            echo "published=no"
          fi
        fi ;;
      stop)
        if [[ -f "$FAKE_FLAGS/stop_fail" ]]; then
          echo "error: 没有可停止的 collectord：$ep" >&2
          exit 1
        fi
        touch "$FAKE_FLAGS/stopped"
        echo "==> 已请求 graceful stop：$ep" ;;
      abort) echo "==> 已请求放弃录制：$ep" ;;
      verify)
        if [[ -f "$FAKE_FLAGS/verify_fail" ]]; then
          echo "INCOMPLETE: 质量门未通过（模拟）" >&2
          exit 1
        fi
        echo "VERIFY_OK" ;;
      *) echo "fake recordctl: unknown $cmd" >&2; exit 2 ;;
    esac
    exit 0
    """
)


def build_fake_repo(tmp_path, flags):
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

    replay = tmp_path / "replay_trajectory_021.jsonl"
    replay.write_text('{"tick":0,"action":[0,0,0,0,0,0,0]}\n')
    return repo, home, flags_dir, calls, replay


def run_record_formal(tmp_path, flags=()):
    repo, home, flags_dir, calls, replay = build_fake_repo(tmp_path, flags)
    env = {
        **os.environ,
        "HOME": str(home),
        "CALLS_LOG": str(calls),
        "FAKE_FLAGS": str(flags_dir),
        # 测试钩子：真实默认 300s，缩短为 2s 使超时路径可测
        "RECORD_FORMAL_COMPLETE_TIMEOUT_S": "2",
    }
    proc = subprocess.run(
        ["bash", str(repo / "deploy" / "lerobot-collect.sh"), "record-formal", "ep001", str(replay)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    call_lines = calls.read_text().splitlines() if calls.exists() else []
    return proc, call_lines


def rezero_call(calls):
    return "panthera workzero rezero --confirm --wait"


def test_verify_ok_auto_rezero(tmp_path):
    proc, calls = run_record_formal(tmp_path, flags=("complete",))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rezero_call(calls) in calls
    assert "record-formal 完成" in proc.stdout
    assert "自动 rezero" in proc.stdout
    assert "上传: ./deploy/lerobot-collect.sh hf-upload" in proc.stdout
    assert "下一步: rezero" not in proc.stdout


def test_verify_fail_still_rezero(tmp_path):
    proc, calls = run_record_formal(tmp_path, flags=("complete", "verify_fail"))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "质量验收失败" in proc.stdout + proc.stderr
    assert "作废重录前删除" in proc.stderr
    # rezero 先行：rezero 在 verify 之前执行（臂安全优先于验收）
    assert calls.index(rezero_call(calls)) < calls.index("recordctl verify ep001")
    # rezero 成功后失败路径不得恢复阻尼锁（臂保持定死锁）
    lock_lines = [i for i, l in enumerate(calls) if l.startswith("panthera teach clutch lock")]
    assert lock_lines and max(lock_lines) < calls.index(rezero_call(calls))


def test_complete_timeout_abort_then_rezero(tmp_path):
    proc, calls = run_record_formal(tmp_path, flags=())
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "超时" in proc.stdout + proc.stderr
    # rezero 先行：超时后才 abort，rezero 早于 abort
    assert calls.index(rezero_call(calls)) < calls.index("recordctl abort ep001")


def test_collectord_dead_without_complete_fails_early(tmp_path):
    # collectord 已退出且无 COMPLETE（收尾失败）→ 立即报错，不等 900s、不 abort
    proc, calls = run_record_formal(tmp_path, flags=("collectord_fail",))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "已退出未 COMPLETE" in proc.stdout + proc.stderr
    assert rezero_call(calls) in calls
    assert "recordctl abort ep001" not in calls


def test_stop_failure_still_rezeros(tmp_path):
    # recordctl stop 失败（collectord 已死）也必须继续 rezero + 后续流程
    proc, calls = run_record_formal(tmp_path, flags=("complete", "stop_fail"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rezero_call(calls) in calls
    assert "record-formal 完成" in proc.stdout


def test_complete_late_final_recheck_succeeds(tmp_path):
    # COMPLETE 在截止后才发布：第 6 步的最终补查应救回成功路径
    proc, calls = run_record_formal(tmp_path, flags=("complete_late",))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rezero_call(calls) in calls
    assert "record-formal 完成" in proc.stdout


def test_rezero_fail_falls_back_to_formal_abort(tmp_path):
    proc, calls = run_record_formal(tmp_path, flags=("complete", "rezero_fail"))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "自动 rezero 失败" in proc.stdout + proc.stderr
    assert "record-formal 中断" in proc.stdout  # formal_abort 的 warn
    # rezero 失败后恢复 teach 阻尼锁（teach clutch lock 在 rezero 调用之后）
    lock_lines = [i for i, l in enumerate(calls) if l.startswith("panthera teach clutch lock")]
    assert lock_lines and max(lock_lines) > calls.index(rezero_call(calls))


# ---------------------------------------------------------------- run-record


def run_run_record(tmp_path, flags=()):
    repo, home, flags_dir, calls, replay = build_fake_repo(tmp_path, flags)
    env = {
        **os.environ,
        "HOME": str(home),
        "CALLS_LOG": str(calls),
        "FAKE_FLAGS": str(flags_dir),
        "RECORD_FORMAL_COMPLETE_TIMEOUT_S": "2",
    }
    proc = subprocess.run(
        ["bash", str(repo / "deploy" / "lerobot-collect.sh"), "run-record", str(replay)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    call_lines = calls.read_text().splitlines() if calls.exists() else []
    return proc, call_lines


def test_run_record_replays_and_directly_rezeros_without_recording(tmp_path):
    proc, calls = run_run_record(tmp_path, flags=())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "run-record 完成" in proc.stdout
    assert rezero_call(calls) in calls
    # 只动臂：完全不碰 recordctl（无录制、无写盘、不造数据）
    assert not any("recordctl" in line for line in calls)
    # 全程不闭爪：不允许出现 --gripper 的 end-lock（只有步骤 1 的无参数 lock）
    assert not any("--gripper" in line for line in calls)
    assert any(line == "panthera teach clutch lock" for line in calls)
    # 回放完成后再 rezero（直接 rezero，无中间 teach 操作）
    play_index = next(i for i, l in enumerate(calls) if l.startswith("panthera teach play"))
    assert play_index < calls.index(rezero_call(calls))


def test_run_record_play_fail_restores_damped_lock(tmp_path):
    proc, calls = run_run_record(tmp_path, flags=("play_fail",))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "run-record 中断" in proc.stdout  # formal_abort 的 warn（run-record 标签）
    assert "teach play 失败" in proc.stdout + proc.stderr
    assert rezero_call(calls) not in calls
    assert any(line == "panthera teach clutch lock" for line in calls)


def test_run_record_rezero_fail_restores_damped_lock(tmp_path):
    proc, calls = run_run_record(tmp_path, flags=("rezero_fail",))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "自动 rezero 失败" in proc.stdout + proc.stderr
    assert "run-record 中断" in proc.stdout
    lock_lines = [i for i, l in enumerate(calls) if l.startswith("panthera teach clutch lock")]
    assert lock_lines and max(lock_lines) > calls.index(rezero_call(calls))
