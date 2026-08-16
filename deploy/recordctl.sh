#!/usr/bin/env bash
# 双终端真机录制控制面：定长 start/watch、graceful stop、abort、status、verify。
#
# Terminal A:
#   ./deploy/recordctl.sh start color-block-000004
# Terminal B:
#   ./deploy/teach-cal.sh lock
#   ./deploy/teach-cal.sh drag
#   ./deploy/recordctl.sh stop color-block-000004
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_root="${PANTHERA_RECORDCTL_STATE_DIR:-$HOME/.cache/panthera-recordctl}"
mkdir -p "$state_root"

task_default='Move the red block from the start area to the target area.'
root_default="${PANTHERA_COLLECTION_ROOT:-$HOME/panthera-data}"
operator_default="${PANTHERA_OPERATOR:-$(id -un)}"

usage() {
    cat <<'EOF'
用法：
  recordctl.sh start EP [选项]       启动定长录制并守着日志（终端 A）
  recordctl.sh watch EP              守着已有录制日志（终端 A）
  recordctl.sh stop EP               请求优雅结束（终端 B）
  recordctl.sh abort EP              放弃本段，保留 FAILED.json（终端 B）
  recordctl.sh status EP             查看进程、日志和产物状态
  recordctl.sh verify EP             验收固定 tick/frame 数

start 选项：
  --duration-s SEC       目标示范时长，默认 30（固定模式）
  --margin-s SEC         采集尾部对齐余量，默认 5
  --variable             变长模式：窗口即实际动作时长，stop 立即优雅收尾
  --max-duration-s SEC   变长模式安全上限，默认 180
  --collection-root DIR  默认 ~/panthera-data
  --task TEXT            默认 color-block 任务文本
  --operator NAME        默认当前用户
  --calibration FILE     默认 <root>/calibration.json
  --identity FILE        默认 <root>/identity.json
  --no-depth             不采 wrist depth（默认开启）
  --detach               启动后不进入日志监看

定长契约：30 s => 901 canonical ticks => 900 training frames。
变长模式：无固定 tick 数，episode 窗口 = stop 时刻之前的全部公共对齐窗口；
--max-duration-s 只是安全上限，到点自动收尾（不再等 stop）。
stop 发送 SIGUSR1；不会 kill 转码，也不会停止 teach/heartbeat。
EOF
}

require_episode() {
    local episode=${1:-}
    [[ -n "$episode" && "$episode" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
        echo "error: episode-id 非法或为空: ${episode:-<empty>}" >&2
        exit 2
    }
}

state_dir() { printf '%s/%s' "$state_root" "$1"; }
pid_file() { printf '%s/pid' "$(state_dir "$1")"; }
log_file() { printf '%s/log' "$(state_dir "$1")"; }
meta_file() { printf '%s/meta.env' "$(state_dir "$1")"; }
root_file() { printf '%s/root' "$(state_dir "$1")"; }

read_pid() {
    local episode=$1 file
    file=$(pid_file "$episode")
    [[ -s "$file" ]] || return 1
    local pid
    pid=$(<"$file")
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$pid"
}

is_collectord_pid() {
    local pid=$1 episode=$2 cmdline
    [[ -d "/proc/$pid" ]] || return 1
    cmdline=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
    [[ "$cmdline" == *armd.collectord* || "$cmdline" == *collectord* ]] || return 1
    [[ "$cmdline" == *"--episode-id"* && "$cmdline" == *"$episode"* ]]
}

active_pid() {
    local episode=$1 pid
    pid=$(read_pid "$episode" 2>/dev/null || true)
    [[ -n "$pid" ]] && is_collectord_pid "$pid" "$episode" || return 1
    printf '%s' "$pid"
}

episode_root() {
    local episode=$1 file
    file=$(root_file "$episode")
    if [[ -s "$file" ]]; then
        cat "$file"
    else
        printf '%s' "$root_default"
    fi
}

episode_final() {
    local episode=$1 root
    root=$(episode_root "$episode")
    printf '%s/episodes/%s' "$root" "$episode"
}

tmp_paths() {
    local episode=$1 root
    root=$(episode_root "$episode")
    find "$root/episodes" -maxdepth 1 -mindepth 1 \
        -name ".${episode}.tmp-*" -print 2>/dev/null || true
}

verify_episode() {
    local episode=$1 final
    final=$(episode_final "$episode")
    [[ -f "$final/COMPLETE" ]] || {
        echo "INCOMPLETE: $final/COMPLETE 不存在" >&2
        return 1
    }
    FINAL="$final" "$repo_root/.venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path

p = Path(os.environ["FINAL"])
e = json.loads((p / "episode.json").read_text())
s = json.loads((p / "sync_report.json").read_text())
f = e.get("fixed_length") or {}
print("episode", e.get("episode_id"))
print("fixed", f.get("enabled"), "| expected_ticks", f.get("canonical_ticks"))
print("ticks", s.get("valid_ticks"), "/", s.get("canonical_ticks"))
print("missing", s.get("missing_frames"))
print("duplicate", s.get("duplicate_frames"))
print("gaps", s.get("sequence_gaps"))
print("overflow", s.get("ring_overflows"))
print("timestamp_regressions", s.get("timestamp_regressions"))
print("depth", e.get("depth"))
print("motion_scope", e.get("motion_scope"))
print("rezero_allowed", (p / "COMPLETE").exists() and e.get("motion_scope") == "task_action_only")
if not e.get("success"):
    raise SystemExit("episode success=false")
if f.get("enabled"):
    expected = int(f.get("canonical_ticks", -1))
    if int(s.get("canonical_ticks", -1)) != expected:
        raise SystemExit("fixed canonical tick count mismatch")
    if expected < 2:
        raise SystemExit("invalid fixed canonical tick count")
    print("training_frames", expected - 1)
for key in ("duplicate_frames", "sequence_gaps", "ring_overflows"):
    if any(int(v) != 0 for v in s.get(key, {}).values()):
        raise SystemExit(f"quality gate failed: {key}")
# missing_frames 与 collectord 门限对齐（≤2%·canonical，至少 3）：
# 缺帧已由 staging 复制上一帧补位（时间线无空洞），此处只审计原始丢帧数。
missing_total = sum(int(v) for v in s.get("missing_frames", {}).values())
missing_tolerance = max(3, round(int(s.get("canonical_ticks", -1)) * 0.02))
if missing_total > missing_tolerance:
    raise SystemExit(f"quality gate failed: missing_frames {missing_total} > {missing_tolerance}")
if int(s.get("timestamp_regressions", -1)) != 0:
    raise SystemExit("quality gate failed: timestamp_regressions")
print("VERIFY_OK")
PY
}

watch_episode() {
    local episode=$1 pid tail_pid final
    local log
    log=$(log_file "$episode")
    final=$(episode_final "$episode")
    [[ -f "$log" ]] || { echo "error: 日志不存在: $log" >&2; return 1; }

    echo "==> 监看 $episode"
    echo "==> 日志：$log"
    echo "==> Ctrl-C 只退出监看；要结束录制请在终端 B 执行 recordctl.sh stop $episode"
    tail -n +1 -F "$log" &
    tail_pid=$!
    cleanup_watch() {
        kill "$tail_pid" 2>/dev/null || true
        wait "$tail_pid" 2>/dev/null || true
    }
    trap cleanup_watch INT TERM EXIT

    while pid=$(active_pid "$episode" 2>/dev/null); do
        sleep 1
    done
    sleep 1
    cleanup_watch
    trap - INT TERM EXIT
    echo "==> collectord 已退出"
    tail -n 8 "$log" || true
    if [[ -f "$final/COMPLETE" ]]; then
        verify_episode "$episode"
    elif [[ -n "$(tmp_paths "$episode")" ]]; then
        echo "FAILED/INCOMPLETE：临时目录仍在，检查 $log" >&2
        return 1
    else
        echo "FAILED/INCOMPLETE：检查 $log" >&2
        return 1
    fi
}

start_episode() {
    local episode=$1
    shift
    local duration_s=30 margin_s=5 task="$task_default" operator="$operator_default"
    local root="$root_default" calibration="" identity="" commit no_depth=0 detach=0
    local variable=0 max_duration_s=180

    while (($#)); do
        case "$1" in
            --duration-s) duration_s=${2:?missing value for --duration-s}; shift 2 ;;
            --margin-s) margin_s=${2:?missing value for --margin-s}; shift 2 ;;
            --variable) variable=1; shift ;;
            --max-duration-s) max_duration_s=${2:?missing value for --max-duration-s}; shift 2 ;;
            --collection-root) root=${2:?missing value for --collection-root}; shift 2 ;;
            --task) task=${2:?missing value for --task}; shift 2 ;;
            --operator) operator=${2:?missing value for --operator}; shift 2 ;;
            --calibration) calibration=${2:?missing value for --calibration}; shift 2 ;;
            --identity) identity=${2:?missing value for --identity}; shift 2 ;;
            --panthera-commit) commit=${2:?missing value for --panthera-commit}; shift 2 ;;
            --no-depth) no_depth=1; shift ;;
            --detach) detach=1; shift ;;
            -h|--help) usage; return 0 ;;
            *) echo "error: 未知 start 选项: $1" >&2; usage >&2; exit 2 ;;
        esac
    done
    [[ "$duration_s" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "error: duration 必须为正数" >&2; exit 2; }
    [[ "$margin_s" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "error: margin 必须为非负数" >&2; exit 2; }
    [[ "$max_duration_s" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "error: max-duration 必须为正数" >&2; exit 2; }
    [[ -n "$calibration" ]] || calibration="$root/calibration.json"
    [[ -n "$identity" ]] || identity="$root/identity.json"
    [[ -n "${commit:-}" ]] || commit=$(git -C "$repo_root" rev-parse HEAD)
    require_episode "$episode"
    [[ "$commit" =~ ^[0-9a-fA-F]{40}$ ]] || { echo "error: commit 必须是完整 40 位 SHA" >&2; exit 2; }
    [[ -x "$repo_root/.venv/bin/collectord" ]] || {
        echo "error: 找不到 $repo_root/.venv/bin/collectord；先在 Pi 上完成 uv sync" >&2
        exit 1
    }
    [[ -f "$calibration" ]] || { echo "error: calibration 不存在: $calibration" >&2; exit 1; }
    [[ -f "$identity" ]] || { echo "error: identity 不存在: $identity" >&2; exit 1; }
    [[ -d "$root/episodes/$episode" ]] && {
        echo "error: episode 已存在: $root/episodes/$episode" >&2; exit 1;
    }
    if pid=$(active_pid "$episode" 2>/dev/null); then
        echo "error: 已有录制进程 $pid 在运行" >&2
        exit 1
    fi
    mkdir -p "$root/episodes"

    local dir log
    dir=$(state_dir "$episode")
    mkdir -p "$dir"
    log=$(log_file "$episode")
    : > "$log"
    rm -f "$(pid_file "$episode")"
    {
        printf 'EPISODE=%q\n' "$episode"
        printf 'ROOT=%q\n' "$root"
        printf 'DURATION_S=%q\n' "$duration_s"
        printf 'MARGIN_S=%q\n' "$margin_s"
        printf 'VARIABLE=%q\n' "$variable"
        printf 'MAX_DURATION_S=%q\n' "$max_duration_s"
        printf 'STARTED_AT=%q\n' "$(date -Is)"
    } >"$(meta_file "$episode")"
    printf '%s\n' "$root" >"$(root_file "$episode")"

    local -a cmd
    cmd=(
        "$repo_root/.venv/bin/collectord"
        --collection-root "$root"
        --episode-id "$episode"
        --task "$task"
        --operator "$operator"
        --panthera-commit "$commit"
        --calibration "$calibration"
        --identity "$identity"
    )
    if ((variable == 1)); then
        cmd+=(--duration-s "$max_duration_s")
    else
        cmd+=(--fixed-duration-s "$duration_s" --fixed-margin-s "$margin_s")
    fi
    if ((no_depth == 0)); then
        cmd+=(--capture-depth)
    fi
    local venv_python="$repo_root/.venv/bin/python"
    local fixed_ticks training_frames
    if ((variable == 0)); then
        fixed_ticks=$("$venv_python" -c 'import math,sys; v=float(sys.argv[1])*30; print(round(v)+1 if math.isclose(v, round(v), abs_tol=1e-6) else (_ for _ in ()).throw(SystemExit("duration must be an integer multiple of 1/30 s")))' "$duration_s")
        training_frames=$((fixed_ticks - 1))
    fi

    echo "==> 启动录制：$episode（$([ "$variable" = 1 ] && echo 变长 || echo 定长)）"
    if ((variable == 1)); then
        echo "==> 变长模式：窗口 = stop 前的全部动作；安全上限 ${max_duration_s}s"
    else
        echo "==> 契约：${duration_s}s -> ${fixed_ticks} canonical ticks -> ${training_frames} training frames"
    fi
    echo "==> 录制余量：${margin_s}s；日志：$log"
    nohup "${cmd[@]}" >"$log" 2>&1 < /dev/null &
    pid=$!
    printf '%s\n' "$pid" >"$(pid_file "$episode")"
    echo "==> collectord PID=$pid"
    sleep 0.3
    if ! is_collectord_pid "$pid" "$episode"; then
        echo "warning: collectord 已退出，查看：tail -20 $log" >&2
        return 1
    fi
    if ((detach == 0)); then
        watch_episode "$episode"
    fi
}

stop_episode() {
    local episode=$1 pid
    require_episode "$episode"
    if ! pid=$(active_pid "$episode" 2>/dev/null); then
        if [[ -f "$(episode_final "$episode")/COMPLETE" ]]; then
            echo "已完成：$episode"
            return 0
        fi
        echo "error: 没有可停止的 collectord：$episode" >&2
        exit 1
    fi
    kill -USR1 "$pid"
    echo "==> 已请求 graceful stop：$episode (PID=$pid)"
    echo "==> 定长模式会等到固定窗口就绪后收尾；不要 kill，终端 A 继续看日志。"
}

abort_episode() {
    local episode=$1 pid
    require_episode "$episode"
    if pid=$(active_pid "$episode" 2>/dev/null); then
        kill -TERM "$pid"
        echo "==> 已请求放弃录制：$episode (PID=$pid)"
    else
        echo "没有运行中的 collectord：$episode"
    fi
}

status_episode() {
    local episode=$1 pid log final
    require_episode "$episode"
    log=$(log_file "$episode")
    final=$(episode_final "$episode")
    echo "episode=$episode"
    if pid=$(active_pid "$episode" 2>/dev/null); then
        echo "process=RUNNING pid=$pid"
    else
        echo "process=NOT_RUNNING"
    fi
    [[ -f "$final/COMPLETE" ]] && echo "published=COMPLETE" || echo "published=no"
    [[ -f "$final/FAILED.json" ]] && echo "failed=FAILED.json" || true
    tmp_paths "$episode" | sed 's/^/temporary=/' || true
    [[ -f "$log" ]] && { echo '--- log tail ---'; tail -n 8 "$log"; }
}

command=${1:-}
shift || true
case "$command" in
    start)
        episode=${1:-}; shift || true
        start_episode "$episode" "$@"
        ;;
    watch)
        episode=${1:-}; require_episode "$episode"; watch_episode "$episode"
        ;;
    stop)
        episode=${1:-}; stop_episode "$episode"
        ;;
    abort)
        episode=${1:-}; abort_episode "$episode"
        ;;
    status)
        episode=${1:-}; status_episode "$episode"
        ;;
    verify)
        episode=${1:-}; require_episode "$episode"; verify_episode "$episode"
        ;;
    -h|--help|help|'')
        usage
        ;;
    *)
        echo "error: 未知命令: $command" >&2
        usage >&2
        exit 2
        ;;
esac
