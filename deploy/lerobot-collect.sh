#!/usr/bin/env bash
# LeRobot 数据采集一行命令分发器（真机 Pi 5）。
#
# 术语：定死锁 = MoveL 终止态（掰不动）；阻尼锁 = teach lock（掰一下复位）。
#
# 五条一行命令（preview 流程）：
#   ./deploy/lerobot-collect.sh gozero                     # 1. 回工作0位（定死锁+开爪）
#   ./deploy/lerobot-collect.sh start-record color-block 021 [--force]  # 2. 终端A：阻尼锁+开始录制（后台；--force 先删旧目录再录）
#   ./deploy/lerobot-collect.sh drag                       # 3. 终端B：恢复手拖
#   ./deploy/lerobot-collect.sh lock --gripper 0.2         # 3. 终端B：闭爪+阻尼锁
#   ./deploy/lerobot-collect.sh end-record [--force]       # 4. 结束录制（变长）→阻尼锁+开爪（--force 跳过已死录制进程收尾）
#   ./deploy/lerobot-collect.sh rezero                     # 5. rezero 回工作0位（定死锁）
#
# 正式录制（把录制的动作用机械臂自己来一遍，开始/结束都 lock）：
#   ./deploy/lerobot-collect.sh record-formal color-block-000011 \
#       ~/panthera-data/preview/color-block_021/replay_trajectory_021.jsonl
#
# 其它：
#   ./deploy/lerobot-collect.sh zero-home                  # 工作0位复位初始0位（收工前必做）
#   ./deploy/lerobot-collect.sh status [EP]                # 电机/lease/episode 状态
#   ./deploy/lerobot-collect.sh verify EP                  # episode 质量验收
#   ./deploy/lerobot-collect.sh hf-upload EP               # 上传 HF（先 verify）
#
# 安全约定：所有运动命令都要求服务端 --confirm；一次只动一步；收工/重启 armd
# 前必须先 zero-home（高位形重启 = 臂坠落事故）。
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
export PANTHERA_ENDPOINT="${PANTHERA_ENDPOINT:-127.0.0.1:50051}"
CLI="$repo_root/.venv/bin/panthera"
STATE_DIR="${PANTHERA_LEROBOCTL_STATE_DIR:-$HOME/.cache/panthera-lerobot-collect}"
PREVIEW_ROOT="${PANTHERA_PREVIEW_OUTPUT_ROOT:-$HOME/panthera-data/preview}"
COLLECTION_ROOT="${PANTHERA_COLLECTION_ROOT:-$HOME/panthera-data}"
OPEN_GRIPPER=1.8        # 开爪 90%（用户定稿）
CLOSE_GRIPPER=0.2       # 闭爪 10%（用户定稿）

die() { echo "error: $*" >&2; exit 1; }
step() { printf '\n\033[1;36m========== %s ==========\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m⚠ %s\033[0m\n' "$*"; }

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

ensure_cli() { [[ -x "$CLI" ]] || die "找不到 $CLI；机械臂回低位后运行 deploy/install-pi5.sh 修复环境"; }

ensure_recorder() {
    "$repo_root/deploy/preview-record.sh" --help >/dev/null 2>&1 \
        || die "preview 录制器依赖检查失败；运行 $repo_root/deploy/preview-record.sh --help 查看原因"
}

ensure_lease() {
    "$CLI" control acquire --client-id session >/dev/null 2>&1 || true
    if ! pgrep -f "[p]anthera control heartbeat" >/dev/null 2>&1; then
        nohup "$CLI" control heartbeat >/dev/null 2>&1 &
        sleep 1
    fi
}

teach_start_lock() {
    local gripper="${1:-}" deadline=$((SECONDS + 15)) start_output lock_output
    # 这里只启动显式离合 teach，然后发 LOCK；绝不发送 DRAG。
    # 从定死锁接管时 armd 的 TeachMotion 首帧直接进入 HOLD。
    if start_output=$("$CLI" teach start --manual-clutch 2>&1); then
        printf '%s\n' "$start_output"
    elif grep -Eq '已有运动正在执行|已有运行中的 teach' <<<"$start_output"; then
        echo "（teach 已在运行，跳过重复启动）"
    else
        printf '%s\n' "$start_output" >&2
        die "teach 启动失败"
    fi
    sleep 0.25
    while ((SECONDS < deadline)); do
        if [[ -n "$gripper" ]]; then
            if lock_output=$("$CLI" teach clutch lock --gripper "$gripper" 2>&1); then
                printf '%s\n' "$lock_output"
                if grep -Eq 'state=hold' <<<"$lock_output"; then
                    return 0
                fi
            else
                printf '%s\n' "$lock_output" >&2
            fi
        elif lock_output=$("$CLI" teach clutch lock 2>&1); then
            printf '%s\n' "$lock_output"
            if grep -Eq 'state=hold' <<<"$lock_output"; then
                return 0
            fi
        else
            printf '%s\n' "$lock_output" >&2
        fi
        sleep 0.75
    done
    die "teach 在 15s 内未进入可锁定状态；请检查 SAFE_HOLD、armd 日志和 E-stop"
}

preflight() {
    "$CLI" state get --json | python3 -c '
import json, sys
data = json.load(sys.stdin)
motors = data if isinstance(data, list) else list(data.get("joints", [])) + [data.get("gripper", {})]
bad = [m.get("name") for m in motors
       if not m.get("valid") or int(m.get("fault", 0)) != 0 or int(m.get("mode", -1)) == 0x0B]
assert len(motors) == 7 and not bad, f"电机异常: {bad}"
print("7 电机正常（valid/fault=0/mode≠0x0B）")' || die "电机前置检查失败"
    "$CLI" control status --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["estop_engaged"] is False, "EStop 已触发，请先复位"
print("控制状态 OK（held=" + str(d["held"]) + "）")' || die "控制状态检查失败"
}

gozero() {
    step "gozero：MoveL 回工作0位 → 定死锁 + 开爪 90%"
    warn "大位移回位：确认工作空间无障碍、人在场、E-stop 可触达"
    ensure_cli; preflight; ensure_lease
    "$CLI" workzero gozero --confirm --wait
    sleep 1
    "$CLI" state get
}

zero_home() {
    step "zero-home：MoveL 回初始0位（低位）→ 快速闭爪 10%"
    warn "大位移回位：确认工作空间无障碍、人在场、E-stop 可触达"
    ensure_cli
    # teach 运行中（阻尼锁）时 MoveL 会被“已有运动”拒绝；先 rezero 回工作0位
    # （定死锁），zero-home 的 MoveL 会自动接管定死锁。探测 lock 本身无副作用：
    # 无 teach 时返回失败，有 teach 时顺便把状态置为 HOLD（更安全）。
    if "$CLI" teach clutch lock >/dev/null 2>&1; then
        echo "==> 检测到 teach 运行（阻尼锁）：先 rezero 回工作0位（定死锁）"
        ensure_lease
        "$CLI" workzero rezero --confirm --wait
    fi
    "$repo_root/deploy/zero-home.sh"
}

start_record() {
    local task="${1:?用法: start-record <任务名> <三位编号> [--max-duration-s 秒] [--force]}"
    local number="${2:?用法: start-record <任务名> <三位编号>}"
    shift 2 || true
    local max_duration_s=600 force=0
    while (($#)); do
        case "$1" in
            --max-duration-s) max_duration_s=${2:?--max-duration-s 需要秒数}; shift 2 ;;
            --force) force=1; shift ;;
            *) die "未知选项: $1" ;;
        esac
    done
    [[ "$task" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "任务名非法: $task"
    [[ "$number" =~ ^[0-9]{3}$ ]] || die "编号必须是三位数字，例如 021"
    ensure_cli; ensure_recorder; preflight; ensure_lease
    local session="${task}_${number}"
    local out="$PREVIEW_ROOT/$session"
    # 目录检查必须在启动 teach 之前：失败退出时不能留下正在运行的 teach。
    if [[ -e "$out" ]]; then
        if ((force)); then
            warn "强制覆盖：删除已有预览目录 $out（先删后录）"
            rm -rf -- "$out"
        else
            die "预览目录已存在，拒绝覆盖: $out（覆盖重录: start-record ... --force）"
        fi
    fi
    step "start-record：定死锁→阻尼锁，然后后台开始预览录制"
    teach_start_lock
    mkdir -p "$STATE_DIR"
    local log="$STATE_DIR/preview-$session.log" pidf="$STATE_DIR/preview-$session.pid"
    : > "$log"
    nohup "$repo_root/deploy/preview-record.sh" "$task" "$number" \
        --duration-s "$max_duration_s" --root "$PREVIEW_ROOT" >"$log" 2>&1 < /dev/null &
    echo $! > "$pidf"
    sleep 2
    kill -0 "$(cat "$pidf")" 2>/dev/null || die "录制进程未存活，看日志: $log"
    echo "==> 录制已启动 pid=$(cat "$pidf")（阻尼锁已锁定）"
    echo "==> 日志: $log"
    echo "==> 终端 B（开始任务动作时才操作）: ./deploy/lerobot-collect.sh drag / lock [--gripper 0.2]"
    echo "==> 动作完成后: ./deploy/lerobot-collect.sh end-record"
}

drag() {
    ensure_cli
    "$CLI" teach clutch drag
}

lock() {
    ensure_cli
    local gripper=""
    while (($#)); do
        case "$1" in
            --gripper) gripper="${2:?--gripper 需要数值}"; shift 2 ;;
            *) die "未知选项: $1" ;;
        esac
    done
    if [[ -n "$gripper" ]]; then
        "$CLI" teach clutch lock --gripper "$gripper"
    else
        "$CLI" teach clutch lock
    fi
}

end_record() {
    local force=0
    while (($#)); do
        case "$1" in
            --force) force=1 ;;
            *) die "未知选项: $1" ;;
        esac
        shift
    done
    ensure_cli
    step "end-record：结束预览录制（变长窗口）→ 阻尼锁 + 开爪"
    local pidf pid session log preview_ok=1
    pidf=$(ls -t "$STATE_DIR"/preview-*.pid 2>/dev/null | head -1 || true)
    if [[ -n "$pidf" ]]; then
        pid=$(cat "$pidf")
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid"
            echo "==> 已请求优雅结束（SIGTERM），等待收尾……"
            local deadline=$((SECONDS + 90))
            while kill -0 "$pid" 2>/dev/null && ((SECONDS < deadline)); do sleep 1; done
            kill -0 "$pid" 2>/dev/null && die "录制进程 90s 内未退出，请人工检查: $pid"
            session=$(basename "$pidf" | sed 's/^preview-//; s/\.pid$//')
            log="$STATE_DIR/preview-$session.log"
            local meta="$PREVIEW_ROOT/$session/preview.json"
            [[ -f "$meta" ]] || die "preview.json 未生成，看日志: $log"
            python3 - "$meta" <<'PY' || preview_ok=0
import json, sys
meta = json.load(open(sys.argv[1], encoding="utf-8"))
assert meta.get("success"), f"preview 失败: {meta}"
assert meta.get("motion_scope") == "task_action_only", "manifest 缺少 action-only 契约"
print(f"==> preview OK: frames={meta['frames']} quality={meta['quality']}")
PY
            tail -n 3 "$log" | sed 's/^/    /'
        elif ((force)); then
            warn "录制进程已退出（pid=$pid），--force 跳过收尾与验收，直接进入阻尼锁 + 开爪"
        else
            die "录制进程已退出（pid=$pid），先看日志: ${pidf%.pid}.log（强制收尾: end-record --force）"
        fi
    elif ((force)); then
        warn "没有进行中的预览录制，--force 直接进入阻尼锁 + 开爪"
    else
        die "没有进行中的预览录制（强制收尾: end-record --force）"
    fi
    # 阻尼锁 + 开爪 90%（脚本开爪，动作指令到此为止）
    "$CLI" teach clutch lock --gripper "$OPEN_GRIPPER"
    echo "==> 已进入阻尼锁 + 开爪 ${OPEN_GRIPPER}（90%）"
    if [[ -n "$session" ]]; then
        ((preview_ok)) || die "preview.json 验收失败，看日志: $log"
    fi
    echo "==> 下一步: ./deploy/lerobot-collect.sh rezero（回工作0位，定死锁）"
}

rezero() {
    step "rezero：开爪松方块 → MoveL 回工作0位 → 定死锁"
    warn "回位运动：确认工作空间无障碍、人在场、E-stop 可触达"
    ensure_cli; ensure_lease
    "$CLI" workzero rezero --confirm --wait
    sleep 1
    "$CLI" state get
}

# ---------------------------------------------------------------- record-formal
_teach_cal_lists() {
    local cal_file="$HOME/.config/panthera-wam/teach-cal.json"
    local kp="${RECORD_FORMAL_KP:-}" kd="${RECORD_FORMAL_KD:-}"
    local fc="${RECORD_FORMAL_FC:-}" fv="${RECORD_FORMAL_FV:-}"
    if [[ -z "$kp" || -z "$kd" || -z "$fc" || -z "$fv" ]]; then
        python3 - "$cal_file" <<'PY'
import json, os, sys
names = ("kp", "kd", "fc", "fv")
defaults = {
    "J1": [0, 0.4, 0.05, 0.02], "J2": [0, 0.55, 0.15, 0.06],
    "J3": [0, 0.6, 0.15, 0.06], "J4": [0, 0.4, 0.15, 0.03],
    "J5": [0, 0.15, 0.02, 0.01], "J6": [0, 0.08, 0.02, 0.01],
}
state = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {
    j: v + [0.85] for j, v in defaults.items()
}
for col, name in enumerate(names):
    print(f"{name}=" + ",".join(str(state[f"J{i}"][col]) for i in range(1, 7)))
PY
    else
        echo "kp=$kp"; echo "kd=$kd"; echo "fc=$fc"; echo "fv=$fv"
    fi
}

record_formal() {
    local episode="${1:?用法: record-formal <episode-id> <replay-jsonl>}"
    local replay="${2:?用法: record-formal <episode-id> <replay-jsonl>}"
    shift 2 || true
    local max_duration_s=180 task="${PANTHERA_FORMAL_TASK:-Move the red block from the start area to the target area.}"
    while (($#)); do
        case "$1" in
            --max-duration-s) max_duration_s=${2:?--max-duration-s 需要秒数}; shift 2 ;;
            --task) task=${2:?--task 需要文本}; shift 2 ;;
            *) die "未知选项: $1" ;;
        esac
    done
    [[ -f "$replay" ]] || die "回放轨迹不存在: $replay"
    ensure_cli; preflight; ensure_lease

    step "record-formal：开始 lock → 变长录制 → teach play 动作 → 结束 lock"
    warn "机械臂将自动回放录制动作：确认人在场、工作空间无障碍、E-stop 可触达"
    # 1) 开始 lock（定死锁→阻尼锁）
    teach_start_lock
    sleep 0.5
    # 2) 退出保持：teach stop 后进入 SAFE_HOLD（默认 10s），期间运动通道仍被
    #    占用；等到 teach 完全退出（clutch lock 探测失败）再启动录制与回放。
    "$CLI" teach stop
    local hold_deadline=$((SECONDS + 30))
    while "$CLI" teach clutch lock >/dev/null 2>&1 && ((SECONDS < hold_deadline)); do
        sleep 1
    done
    if "$CLI" teach clutch lock >/dev/null 2>&1; then
        die "teach SAFE_HOLD 30s 内未退出，稍后重试本命令"
    fi
    echo "==> teach 已退出，运动通道空闲"
    # 3) 启动变长正式录制
    ./deploy/recordctl.sh start "$episode" --variable --max-duration-s "$max_duration_s" \
        --task "$task" --detach
    sleep 3
    ./deploy/recordctl.sh status "$episode" | grep -q "process=RUNNING" \
        || die "collectord 未在运行，看日志: $STATE_DIR/../panthera-recordctl/$episode/log"
    # 4) teach play：把录制的动作来一遍（参数来自 teach-cal.json / 环境变量覆盖）
    local params
    params=$(_teach_cal_lists)
    local kp kd fc fv
    kp=$(printf '%s\n' "$params" | sed -n 's/^kp=//p')
    kd=$(printf '%s\n' "$params" | sed -n 's/^kd=//p')
    fc=$(printf '%s\n' "$params" | sed -n 's/^fc=//p')
    fv=$(printf '%s\n' "$params" | sed -n 's/^fv=//p')
    echo "==> teach play 参数: kp=$kp kd=$kd fc=$fc fv=$fv"
    echo "==> 回放轨迹: $replay"
    "$CLI" teach play "$replay" --mode mit \
        --kp "$kp" --kd "$kd" --fc "$fc" --fv "$fv" \
        || die "teach play 失败（若提示已有运动，说明 SAFE_HOLD 未结束，稍后重试本命令）"
    # 5) 结束 lock（阻尼锁 + 闭爪 10%）
    teach_start_lock "$CLOSE_GRIPPER"
    # 6) 优雅结束录制 + 验收
    ./deploy/recordctl.sh stop "$episode"
    local deadline=$((SECONDS + 300))
    while ((SECONDS < deadline)); do
        ./deploy/recordctl.sh status "$episode" 2>/dev/null | grep -q "published=COMPLETE" && break
        sleep 5
    done
    ./deploy/recordctl.sh verify "$episode" || die "episode 质量验收失败"
    echo "==> record-formal 完成: $COLLECTION_ROOT/episodes/$episode"
    echo "==> 下一步: ./deploy/lerobot-collect.sh rezero；上传: ./deploy/lerobot-collect.sh hf-upload $episode"
}

# ---------------------------------------------------------------- status/verify/hf
status() {
    ensure_cli
    if [[ ${1:-} != "" ]]; then
        ./deploy/recordctl.sh status "$1"
        return
    fi
    preflight || true
    "$CLI" workzero show | head -5 || true
    "$CLI" state get
}

verify() {
    ./deploy/recordctl.sh verify "${1:?用法: verify <episode-id>}"
}

hf_upload() {
    local episode="${1:?用法: hf-upload <episode-id>}"
    verify "$episode"
    local ep_dir="$COLLECTION_ROOT/episodes/$episode"
    [[ -f "$ep_dir/COMPLETE" ]] || die "episode 不存在或未 COMPLETE: $ep_dir"
    export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
    echo "==> 上传 $ep_dir → winbeau/fastwam-lerobot（endpoint=$HF_ENDPOINT）"
    "$repo_root/.venv/bin/panthera-hf-upload-episode" "$ep_dir"
    echo "==> 请记录输出中的 Hub revision（40 位），随实验一起留档"
}

command=${1:-}
shift || true
case "$command" in
    gozero) gozero ;;
    zero-home) zero_home ;;
    start-record) start_record "$@" ;;
    drag) drag ;;
    lock) lock "$@" ;;
    end-record) end_record ;;
    rezero) rezero ;;
    record-formal) record_formal "$@" ;;
    status) status "$@" ;;
    verify) verify "$@" ;;
    hf-upload) hf_upload "$@" ;;
    -h|--help|help|'') usage 0 ;;
    *) echo "error: 未知命令: $command" >&2; usage 2 ;;
esac
