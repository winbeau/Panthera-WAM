#!/usr/bin/env bash
# Panthera-WAM Pi 5 设备恢复：服务重启优先，udev/ttyACM 映射刷新为辅。
#
# 这个脚本只处理设备生命周期和只读验收：
#   - 不获取或释放 lease；
#   - 不发送 CAN/电机/夹爪/WorkZero/Policy 命令；
#   - 不修改 encoder zero、配置文件或 udev 规则内容；
#   - 默认拒绝 active lease、teach、collectord 和已知运动客户端。
set -Eeuo pipefail
IFS=$'\n\t'

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

TARGET=""
DRY_RUN=0
CONFIRMED=0
FORCE=0
TIMEOUT_S="${PANTHERA_DEVICE_REPAIR_TIMEOUT_S:-20}"
MIN_TTYACM="${PANTHERA_REPAIR_MIN_TTYACM:-4}"
LOG_PATH="${PANTHERA_DEVICE_REPAIR_LOG:-}"
MAP_PATH="${PANTHERA_DEVICE_MAP_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/panthera/ttyacm-map.tsv}"
PHASE="init"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/panthera"
CLI_BIN="$repo_root/.venv/bin/panthera"
LAST_RC=0
CLI_TIMEOUT_S=5
LOCK_FILE="$STATE_DIR/device-repair.lock"

usage() {
    cat <<'EOF'
用法：
  ./deploy/repair-devices.sh TARGET [选项]

TARGET：
  c920e       修复 Logitech C920e / overhead-camera.service
  realsense   修复 Intel RealSense D405 / camerad.service
  can         修复 Panthera CAN/ttyACM / armd.service
  all         按安全依赖顺序修复全部设备

选项：
  --dry-run             只读预检并打印计划，不停止/启动服务、不刷新 udev
  --yes                 强制修复：跳过安全门（lease/运动客户端检查），
                        无论什么状态直接执行停止→udev 刷新→重启；操作者
                        自行确认人在场、扶臂、E-stop 可触达（高位重启会坠臂）
  --log FILE            日志文件；默认 ~/.local/state/panthera/device-repair-*.log
  --map-file FILE       ttyACM 只读映射快照；默认 ~/.local/state/panthera/ttyacm-map.tsv
  --timeout-s SEC       每个等待阶段的超时，默认 20
  --min-ttyacm N        CAN 验收的最少 ttyACM 数量，默认 4
  -h, --help            显示帮助

示例：
  ./deploy/repair-devices.sh c920e --dry-run
  ./deploy/repair-devices.sh c920e --yes
  ./deploy/repair-devices.sh realsense --yes --log /tmp/d405-repair.log
  ./deploy/repair-devices.sh can --yes
  ./deploy/repair-devices.sh all --yes

退出码：
  0  修复并通过只读验收
  2  参数错误或缺少 --yes
  3  环境/权限/依赖不满足
  4  安全门拒绝（lease、运动、teach 或 collectord）
  5  服务或 udev 操作失败
  6  设备仍不可用或只读验收失败
  7  等待服务/设备超时
  8  dry-run 完成（没有执行修改）
EOF
}

fail() {
    local code=$1
    shift
    if [[ -n "${LOG_PATH:-}" ]]; then
        log "result=failed rc=$code message=$(printf '%q ' "$@")"
    else
        printf 'repair-devices: result=failed rc=%s message=%q\n' "$code" "$*" >&2
    fi
    printf 'repair-devices: %s\n' "$*" >&2
    exit "$code"
}

cmd_string() {
    local item rendered=""
    for item in "$@"; do
        printf -v item '%q' "$item"
        rendered+=" $item"
    done
    printf '%s' "${rendered# }"
}

log() {
    local line
    line="$(date -Is) target=$TARGET phase=$PHASE $*"
    printf '%s\n' "$line" | tee -a "$LOG_PATH"
}

log_multiline() {
    local text=$1 lines first
    [[ -z "$text" ]] && return 0
    lines=$(printf '%s\n' "$text" | grep -c . || true)
    if ((lines <= 1)); then
        log "output=$(printf '%q' "$text")"
        return 0
    fi
    first=$(printf '%s\n' "$text" | head -1)
    # 多行输出只打印一行摘要（控制台不刷屏），完整内容原样追加进日志文件。
    log "output=$(printf '%q' "$first") …(共 $lines 行，完整输出已追加到日志文件)"
    printf '%s\n' "$text" >>"$LOG_PATH"
}

run_cmd() {
    local label=$1
    shift
    log "action=$label command=$(cmd_string "$@")"
    if ((DRY_RUN)); then
        LAST_RC=0
        log "result=would-run"
        return 0
    fi
    local rc=0
    set +e
    "$@" >>"$LOG_PATH" 2>&1
    rc=$?
    set -e
    LAST_RC=$rc
    if ((rc == 0)); then
        log "result=ok action=$label"
    else
        log "result=failed action=$label rc=$rc"
    fi
    return "$rc"
}

capture_cmd() {
    local variable=$1
    local label=$2
    shift 2
    log "action=$label command=$(cmd_string "$@")"
    if ((DRY_RUN)); then
        printf -v "$variable" '%s' ''
        LAST_RC=125
        log "result=not-executed"
        return 125
    fi
    local output rc=0
    set +e
    output=$("$@" 2>&1)
    rc=$?
    set -e
    log_multiline "$output"
    printf -v "$variable" '%s' "$output"
    LAST_RC=$rc
    if ((rc == 0)); then
        log "result=ok action=$label"
    else
        log "result=failed action=$label rc=$rc"
    fi
    return "$rc"
}

run_privileged() {
    local label=$1
    shift
    if ((EUID == 0)); then
        run_cmd "$label" "$@"
    else
        run_cmd "$label" sudo "$@"
    fi
}

normalize_target() {
    case "$TARGET" in
        c920e|overhead) TARGET=c920e ;;
        realsense|d405|wrist) TARGET=realsense ;;
        can|arm|armd) TARGET=can ;;
        all) TARGET=all ;;
        *) fail 2 "未知目标：$TARGET（可选 c920e、realsense、can、all）" ;;
    esac
}

parse_args() {
    while (($#)); do
        case "$1" in
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --yes|--confirm|--force)
                CONFIRMED=1
                FORCE=1
                shift
                ;;
            --log)
                (($# >= 2)) || { usage >&2; exit 2; }
                LOG_PATH=$2
                shift 2
                ;;
            --map-file)
                (($# >= 2)) || { usage >&2; exit 2; }
                MAP_PATH=$2
                shift 2
                ;;
            --timeout-s|--timeout)
                (($# >= 2)) || { usage >&2; exit 2; }
                TIMEOUT_S=$2
                shift 2
                ;;
            --min-ttyacm)
                (($# >= 2)) || { usage >&2; exit 2; }
                MIN_TTYACM=$2
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            --)
                shift
                break
                ;;
            -*)
                usage >&2
                exit 2
                ;;
            *)
                if [[ -n "$TARGET" ]]; then
                    usage >&2
                    exit 2
                fi
                TARGET=$1
                shift
                ;;
        esac
    done

    [[ -n "$TARGET" ]] || { usage >&2; exit 2; }
    [[ "$TIMEOUT_S" =~ ^[1-9][0-9]*$ ]] || fail 2 "--timeout-s 必须是正整数"
    [[ "$MIN_TTYACM" =~ ^[1-9][0-9]*$ ]] || fail 2 "--min-ttyacm 必须是正整数"
    TIMEOUT_S=$((10#$TIMEOUT_S))
    MIN_TTYACM=$((10#$MIN_TTYACM))
    ((TIMEOUT_S <= 600)) || fail 2 "--timeout-s 不能超过 600"
    ((MIN_TTYACM <= 64)) || fail 2 "--min-ttyacm 不能超过 64"
    CLI_TIMEOUT_S=$((TIMEOUT_S < 5 ? TIMEOUT_S : 5))
    normalize_target
    if ((DRY_RUN == 0 && CONFIRMED == 0)); then
        fail 2 "真实执行必须显式传入 --yes；先用 --dry-run 查看计划"
    fi
}

init_log() {
    umask 077
    mkdir -p "$(dirname "$MAP_PATH")" "$STATE_DIR" 2>/dev/null || true
    if [[ -z "$LOG_PATH" ]]; then
        LOG_PATH="$STATE_DIR/device-repair-$(date +%Y%m%dT%H%M%S)-$$.log"
    fi
    mkdir -p "$(dirname "$LOG_PATH")" 2>/dev/null || {
        printf 'repair-devices: 无法创建日志目录：%s\n' "$(dirname "$LOG_PATH")" >&2
        exit 3
    }
    touch "$LOG_PATH" 2>/dev/null || {
        printf 'repair-devices: 无法写入日志：%s\n' "$LOG_PATH" >&2
        exit 3
    }
    chmod 0600 "$LOG_PATH" 2>/dev/null || true
    log "event=start dry_run=$DRY_RUN repo=$(printf '%q' "$repo_root") log=$(printf '%q' "$LOG_PATH") map=$(printf '%q' "$MAP_PATH")"
}

need_command() {
    local command_name=$1
    command -v "$command_name" >/dev/null 2>&1 || fail 3 "缺少命令：$command_name"
}

service_name_for_target() {
    case "$1" in
        c920e) printf '%s' overhead-camera.service ;;
        realsense) printf '%s' camerad.service ;;
        can) printf '%s' armd.service ;;
        *) return 1 ;;
    esac
}

service_exists() {
    systemctl --user cat "$1" >/dev/null 2>&1
}

service_state() {
    local state
    # 注意：systemctl is-active 对 inactive/failed 状态返回非零退出码，
    # 但输出文本是可靠的；只有无输出（dbus/命令失败）才视为 query-failed。
    set +e
    state=$(systemctl --user is-active "$1" 2>/dev/null)
    set -e
    if [[ -n "$state" ]]; then
        printf '%s' "$state"
    else
        printf '%s' query-failed
    fi
}

port_listening() {
    local port=$1
    ss -ltnH 2>/dev/null | awk -v port=":$port" '$4 ~ (port "$") { found=1 } END { exit !found }'
}

wait_service_inactive() {
    local service=$1
    local attempts=$((TIMEOUT_S * 10))
    local state
    if ((DRY_RUN)); then
        log "wait=inactive service=$service result=would-wait"
        return 0
    fi
    for ((attempts = TIMEOUT_S * 10; attempts > 0; attempts--)); do
        state=$(service_state "$service")
        if [[ "$state" == "inactive" || "$state" == "failed" || "$state" == "unknown" ]]; then
            log "wait=inactive service=$service state=$state result=ok"
            return 0
        fi
        # systemd 查询瞬时失败（停止瞬间常见）：重试，不立即报错
        log "wait=inactive service=$service state=$state result=retry"
        sleep 0.1
    done
    log "wait=inactive service=$service result=timeout state=${state:-unknown}"
    return 7
}

wait_service_active() {
    local service=$1
    local port=${2:-}
    local attempts
    local state
    if ((DRY_RUN)); then
        log "wait=active service=$service port=${port:-none} result=would-wait"
        return 0
    fi
    for ((attempts = TIMEOUT_S * 10; attempts > 0; attempts--)); do
        state=$(service_state "$service")
        if [[ "$state" == "active" ]] && { [[ -z "$port" ]] || port_listening "$port"; }; then
            log "wait=active service=$service port=${port:-none} result=ok"
            return 0
        fi
        sleep 0.1
    done
    log "wait=active service=$service port=${port:-none} result=timeout state=${state:-unknown}"
    return 7
}

wait_port_gone() {
    local port=$1
    local attempts
    if ((DRY_RUN)); then
        log "wait=port-gone port=$port result=would-wait"
        return 0
    fi
    for ((attempts = TIMEOUT_S * 10; attempts > 0; attempts--)); do
        if ! port_listening "$port"; then
            log "wait=port-gone port=$port result=ok"
            return 0
        fi
        sleep 0.1
    done
    log "wait=port-gone port=$port result=timeout"
    return 7
}

stop_service() {
    local service=$1
    local port=${2:-}
    PHASE=stop
    local state
    state=$(service_state "$service")
    case "$state" in
        inactive|failed|unknown)
            log "service=$service state=$state action=stop result=already-inactive"
            return 0
            ;;
        query-failed)
            log "service=$service action=stop result=query-failed"
            return 5
            ;;
    esac
    run_cmd "stop-$service" systemctl --user stop "$service" || return 5
    wait_service_inactive "$service" || return $?
    [[ -z "$port" ]] || wait_port_gone "$port" || return $?
    return 0
}

start_service() {
    local service=$1
    local port=${2:-}
    PHASE=start
    run_cmd "start-$service" systemctl --user start "$service" || return 5
    wait_service_active "$service" "$port" || return $?
    return 0
}

prepare_privilege() {
    ((DRY_RUN)) && return 0
    if ((EUID != 0)); then
        # sudo -v 在密码未缓存时通过控制终端提示输入（/dev/tty，与 stderr
        # 是否重定向无关）；密码已缓存或 NOPASSWD 时静默成功。不再硬性
        # 检查 fd0/fd2 是否为 TTY——pi 终端的 stderr 被重定向时该检查会
        # 误拒绝。
        if ! sudo -v; then
            fail 3 "udev 刷新需要 sudo 密码：请先在 Pi 终端运行 sudo -v 缓存密码，再执行本脚本"
        fi
        log "action=sudo-validate result=ok"
    fi
}

print_process_guard() {
    local matches
    matches=$(pgrep -af '[c]ollectord|[t]each-cal\.sh|[l]ease-heartbeat\.py|[p]anthera.*(workzero|movej?|jog|policy)' 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
        log "safety=active-client matches=$(printf '%q' "$matches")"
        return 1
    fi
    return 0
}

json_control_held() {
    python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("held") else 1)' <<<"$1"
}

json_stationary() {
    python3 -c 'import json,sys; d=json.load(sys.stdin); v=[abs(float(x.get("velocity",0.0))) for x in d]; raise SystemExit(0 if max(v or [0.0]) <= 0.05 else 1)' <<<"$1"
}

check_arm_safety() {
    local checkpoint=${1:-preflight}
    local control_json="" state_json="" armd_state
    PHASE=$checkpoint
    if ((FORCE)); then
        log "safety=bypassed force=--yes checkpoint=$checkpoint"
        return 0
    fi
    armd_state=$(service_state armd.service)

    if capture_cmd control_json control-status env \
        PANTHERA_ENDPOINT=127.0.0.1:50051 \
        timeout "${CLI_TIMEOUT_S}s" \
        "$CLI_BIN" control status --json; then
        if json_control_held "$control_json"; then
            log "safety=active-lease result=blocked"
            return 4
        fi
    else
        if ((DRY_RUN)); then
            log "safety=control-status result=not-checked-dry-run"
            return 0
        fi
        if [[ "$armd_state" =~ ^(inactive|failed|unknown)$ ]] && ! port_listening 50051; then
            log "safety=control-status result=no-active-armd-endpoint state=$armd_state"
            return 0
        fi
        log "safety=control-status result=unavailable state=$armd_state rc=$LAST_RC"
        return 4
    fi

    if capture_cmd state_json state-safety env \
        PANTHERA_ENDPOINT=127.0.0.1:50051 \
        timeout "${CLI_TIMEOUT_S}s" \
        "$CLI_BIN" state get --json; then
        if ! json_stationary "$state_json"; then
            log "safety=moving-state result=blocked threshold_rad_s=0.05"
            return 4
        fi
    else
        if ((DRY_RUN)); then
            log "safety=state result=not-checked-dry-run"
            return 0
        fi
        log "safety=state result=unavailable state=$armd_state rc=$LAST_RC"
        return 4
    fi
    log "safety=arm result=stationary-no-lease"
    return 0
}

safety_preflight() {
    PHASE=preflight
    local service service_status
    for service in armd.service camerad.service overhead-camera.service; do
        if service_exists "$service"; then
            service_status=$(service_state "$service")
            log "service=$service state=$service_status"
            [[ "$service_status" != query-failed ]] || fail 3 "无法查询 systemd user unit：$service"
        else
            log "service=$service result=missing-unit"
            fail 3 "缺少 systemd user unit：$service"
        fi
    done

    if ((FORCE)); then
        log "safety=bypassed force=--yes（跳过 active-client 检查）"
    elif ! print_process_guard; then
        if ((DRY_RUN)); then
            log "safety=blocked result=would-refuse active teach/collectord/motion client"
        else
            fail 4 "检测到 teach、collectord、lease-heartbeat 或运动客户端；请先优雅结束后重试"
        fi
    fi

    if ! check_arm_safety preflight-arm; then
        if ((DRY_RUN)); then
            log "safety=arm result=would-refuse"
        else
            fail 4 "不能确认机械臂静止且无 lease，未执行服务重启"
        fi
    fi

    if [[ "$TARGET" == c920e || "$TARGET" == all ]]; then
        log_aliases c920e
    fi
    if [[ "$TARGET" == realsense || "$TARGET" == all ]]; then
        log_aliases realsense
    fi
    log_ttyacm
    if ((DRY_RUN)); then
        log "safety=result:not-executed-dry-run"
    else
        log "safety=passed"
    fi
}

alias_path() {
    case "$1" in
        c920e) printf '%s' "$HOME/camera-devices/c920e" ;;
        realsense-depth) printf '%s' "$HOME/camera-devices/realsense-depth" ;;
        realsense-infrared) printf '%s' "$HOME/camera-devices/realsense-infrared" ;;
        realsense-color) printf '%s' "$HOME/camera-devices/realsense-color" ;;
        *) return 1 ;;
    esac
}

log_aliases() {
    local group=$1 name path resolved
    case "$group" in
        c920e)
            for name in c920e; do
                path=$(alias_path "$name")
                if [[ -L "$path" ]]; then
                    resolved=$(readlink -f "$path" 2>/dev/null || true)
                    log "alias=$path required=true symlink=true resolved=${resolved:-unresolved} char=$([[ -c "$path" ]] && echo true || echo false)"
                else
                    log "alias=$path required=true symlink=false resolved=missing"
                fi
            done
            ;;
        realsense)
            for name in realsense-depth realsense-infrared realsense-color; do
                path=$(alias_path "$name")
                if [[ -L "$path" ]]; then
                    resolved=$(readlink -f "$path" 2>/dev/null || true)
                    log "alias=$path required=false advisory=rsusb-v4l2 symlink=true resolved=${resolved:-unresolved} char=$([[ -c "$path" ]] && echo true || echo false)"
                else
                    log "alias=$path required=false advisory=rsusb-v4l2 symlink=false resolved=missing"
                fi
            done
            ;;
    esac
}

list_ttyacm() {
    local path
    for path in /dev/ttyACM*; do
        [[ -e "$path" ]] || continue
        printf '%s\n' "$path"
    done
}

log_ttyacm() {
    local count
    count=$(list_ttyacm | wc -l)
    log "ttyacm=count:$count paths=$(list_ttyacm | tr '\n' ' ')"
}

write_ttyacm_map() {
    PHASE=map
    local map_dir tmp path props vendor model serial id_path devlinks
    map_dir=$(dirname "$MAP_PATH")
    if ((DRY_RUN)); then
        log "action=write-ttyacm-map path=$MAP_PATH result=would-write"
        return 0
    fi
    mkdir -p "$map_dir" 2>/dev/null || {
        log "action=write-ttyacm-map path=$MAP_PATH result=warning mkdir-failed"
        return 0
    }
    tmp=$(mktemp "${MAP_PATH}.tmp.XXXXXX") || {
        log "action=write-ttyacm-map path=$MAP_PATH result=warning mktemp-failed"
        return 0
    }
    {
        printf 'captured_at\tdevice\tvendor\tmodel\tserial\tid_path\tdevlinks\n'
        for path in /dev/ttyACM*; do
            [[ -e "$path" ]] || continue
            props=$(udevadm info --query=property --name="$path" 2>/dev/null || true)
            vendor=$(awk -F= '$1 == "ID_VENDOR" {print substr($0,index($0,"=")+1); exit}' <<<"$props")
            model=$(awk -F= '$1 == "ID_MODEL" {print substr($0,index($0,"=")+1); exit}' <<<"$props")
            serial=$(awk -F= '$1 == "ID_SERIAL_SHORT" {print substr($0,index($0,"=")+1); exit}' <<<"$props")
            id_path=$(awk -F= '$1 == "ID_PATH" {print substr($0,index($0,"=")+1); exit}' <<<"$props")
            devlinks=$(awk -F= '$1 == "DEVLINKS" {print substr($0,index($0,"=")+1); exit}' <<<"$props")
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$(date -Is)" "$path" "$vendor" "$model" "$serial" "$id_path" "$devlinks"
        done
    } >"$tmp"
    if ! chmod 0600 "$tmp"; then
        rm -f "$tmp"
        log "action=write-ttyacm-map path=$MAP_PATH result=warning chmod-failed"
        return 0
    fi
    if ! mv -f "$tmp" "$MAP_PATH"; then
        rm -f "$tmp"
        log "action=write-ttyacm-map path=$MAP_PATH result=warning move-failed"
        return 0
    fi
    log "action=write-ttyacm-map path=$MAP_PATH result=ok"
}

refresh_udev() {
    PHASE=udev
    run_privileged udev-reload udevadm control --reload-rules || return 5
    case "$TARGET" in
        c920e)
            run_privileged udev-trigger-video udevadm trigger --subsystem-match=video4linux || return 5
            ;;
        realsense)
            run_privileged udev-trigger-realsense-usb udevadm trigger \
                --subsystem-match=usb --attr-match=idVendor=8086 --attr-match=idProduct=0b5b || return 5
            run_privileged udev-trigger-realsense-video udevadm trigger \
                --subsystem-match=video4linux || return 5
            ;;
        can)
            run_privileged udev-trigger-ttyacm udevadm trigger \
                --subsystem-match=tty --sysname-match='ttyACM*' || return 5
            ;;
        all)
            run_privileged udev-trigger-ttyacm udevadm trigger \
                --subsystem-match=tty --sysname-match='ttyACM*' || return 5
            run_privileged udev-trigger-video udevadm trigger \
                --subsystem-match=video4linux || return 5
            run_privileged udev-trigger-realsense-usb udevadm trigger \
                --subsystem-match=usb --attr-match=idVendor=8086 --attr-match=idProduct=0b5b || return 5
            ;;
    esac
    run_privileged udev-settle udevadm settle --timeout="$TIMEOUT_S" || return 5
    write_ttyacm_map
    log_aliases c920e
    log_aliases realsense
    return 0
}

cli_capture_camera_status() {
    local source=$1 variable=$2
    if [[ "$source" == overhead ]]; then
        capture_cmd "$variable" "camera-status-overhead" env \
            PANTHERA_OVERHEAD_CAMERA_ENDPOINT=127.0.0.1:50053 \
            timeout "${CLI_TIMEOUT_S}s" \
            "$CLI_BIN" camera status \
            --source overhead --json
    else
        capture_cmd "$variable" "camera-status-wrist" env \
            PANTHERA_CAMERA_ENDPOINT=127.0.0.1:50052 \
            timeout "${CLI_TIMEOUT_S}s" \
            "$CLI_BIN" camera status \
            --source wrist --json
    fi
}

camera_json_healthy() {
    local expected_role=$1
    python3 -c '
import json, sys
data = json.load(sys.stdin)
if not data.get("available") or not data.get("streaming"):
    raise SystemExit(1)
if not str(data.get("role", "")).endswith(sys.argv[1]):
    raise SystemExit(2)
if sys.argv[1] == "WRIST" and data.get("serial") not in {"", "260422273428"}:
    raise SystemExit(3)
' "$expected_role" <<<"$2"
}

wait_camera() {
    local source=$1 expected_role=$2 status_json=""
    local attempts
    if ((DRY_RUN)); then
        log "wait=camera source=$source result=would-wait"
        return 0
    fi
    for ((attempts = TIMEOUT_S; attempts > 0; attempts--)); do
        if cli_capture_camera_status "$source" status_json \
            && camera_json_healthy "$expected_role" "$status_json"; then
            log "camera=$source result=available-streaming"
            return 0
        fi
        sleep 1
    done
    log "camera=$source result=timeout-or-unhealthy"
    return 7
}

snapshot_camera() {
    local source=$1 stream=$2 suffix=$3 output
    if ((DRY_RUN)); then
        log "action=snapshot source=$source stream=$stream result=would-read-one-frame"
        return 0
    fi
    if ! output=$(mktemp "/tmp/panthera-device-repair-${source}-${stream}-XXXXXX.${suffix}"); then
        log "action=snapshot source=$source stream=$stream result=mktemp-failed"
        return 5
    fi
    if [[ "$source" == overhead ]]; then
        run_cmd "snapshot-$source" env \
            PANTHERA_OVERHEAD_CAMERA_ENDPOINT=127.0.0.1:50053 \
            timeout "${CLI_TIMEOUT_S}s" \
            "$CLI_BIN" camera snapshot \
            --source overhead --out "$output" || { ((LAST_RC == 124)) && return 7; return 6; }
    else
        run_cmd "snapshot-$source-$stream" env \
            PANTHERA_CAMERA_ENDPOINT=127.0.0.1:50052 \
            timeout "${CLI_TIMEOUT_S}s" \
            "$CLI_BIN" camera snapshot \
            --source wrist --stream "$stream" --out "$output" || { ((LAST_RC == 124)) && return 7; return 6; }
    fi
    [[ -s "$output" ]] || {
        rm -f "$output" "$output.json"
        log "snapshot=$output result=empty"
        return 6
    }
    log "snapshot=$output result=ok bytes=$(stat -c '%s' "$output")"
    rm -f "$output" "$output.json"
}

verify_c920e() {
    PHASE=verify
    local alias
    alias=$(alias_path c920e)
    [[ -L "$alias" && -c "$alias" ]] || {
        log "verify=c920e alias=$alias result=failed"
        return 6
    }
    log "verify=c920e alias=$alias result=character-device"
    if ! run_cmd v4l2-info v4l2-ctl --device="$alias" --all; then
        log "verify=c920e v4l2-info result=warning service-snapshot-is-authoritative"
    fi
    wait_camera overhead OVERHEAD || return $?
    snapshot_camera overhead jpeg jpg || return $?
    return 0
}

verify_realsense() {
    PHASE=verify
    if ! run_cmd lsusb-realsense lsusb -d 8086:0b5b; then
        log "verify=realsense usb=result=failed"
        return 6
    fi
    log_aliases realsense
    wait_camera wrist WRIST || return $?
    snapshot_camera wrist depth pgm || return $?
    snapshot_camera wrist color ppm || return $?
    return 0
}

json_arm_healthy() {
    python3 -c '
import json, sys
data = json.load(sys.stdin)
if data.get("sim") is not False or data.get("hardware_connected") is not True:
    raise SystemExit(1)
' <<<"$1"
}

json_state_healthy() {
    python3 -c '
import json, sys
data = json.load(sys.stdin)
if len(data) < 7:
    print(f"motor_count={len(data)}", file=sys.stderr)
    raise SystemExit(1)
bad=[]
for item in data:
    mode=int(item.get("mode", -1))
    if not item.get("valid") or int(item.get("fault", 0)) != 0 or mode != 0x15:
        bad.append({"name": item.get("name"), "id": item.get("motor_id"), "mode": f"0x{mode:02X}", "fault": item.get("fault"), "valid": item.get("valid")})
if bad:
    print(json.dumps({"bad_motors": bad}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(2)
velocities=[abs(float(item.get("velocity", 0.0))) for item in data]
if max(velocities or [0.0]) > 0.05:
    print(json.dumps({"max_velocity": max(velocities)}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(3)
' <<<"$1"
}

verify_can() {
    PHASE=verify
    local count daemon_json state_json control_json
    count=$(list_ttyacm | wc -l)
    log "verify=ttyacm count=$count minimum=$MIN_TTYACM"
    ((count >= MIN_TTYACM)) || return 6
    if ! run_cmd lsusb-panthera lsusb -d caf1:ffff; then
        log "verify=panthera-usb result=failed"
        return 6
    fi
    capture_cmd daemon_json daemon-status env \
        PANTHERA_ENDPOINT=127.0.0.1:50051 \
        timeout "${CLI_TIMEOUT_S}s" \
        "$CLI_BIN" daemon status --json || { ((LAST_RC == 124)) && return 7; return 6; }
    if ! json_arm_healthy "$daemon_json"; then
        log "verify=armd daemon result=failed"
        return 6
    fi
    capture_cmd state_json state-get env \
        PANTHERA_ENDPOINT=127.0.0.1:50051 \
        timeout "${CLI_TIMEOUT_S}s" \
        "$CLI_BIN" state get --json || { ((LAST_RC == 124)) && return 7; return 6; }
    if ! json_state_healthy "$state_json"; then
        log "verify=motors result=failed expected_mode=0x15"
        return 6
    fi
    capture_cmd control_json control-status-post env \
        PANTHERA_ENDPOINT=127.0.0.1:50051 \
        timeout "${CLI_TIMEOUT_S}s" \
        "$CLI_BIN" control status --json || { ((LAST_RC == 124)) && return 7; return 6; }
    if json_control_held "$control_json"; then
        log "verify=lease result=failed held=true"
        return 6
    fi
    log "verify=armd result=hardware-connected-stationary-no-lease"
    return 0
}

repair_c920e() {
    check_arm_safety pre-stop-device || return $?
    prepare_privilege
    stop_service overhead-camera.service 50053 || return $?
    refresh_udev || return $?
    start_service overhead-camera.service 50053 || return $?
    verify_c920e
}

repair_realsense() {
    check_arm_safety pre-stop-device || return $?
    prepare_privilege
    stop_service camerad.service 50052 || return $?
    refresh_udev || return $?
    start_service camerad.service 50052 || return $?
    verify_realsense
}

arm_already_healthy() {
    # 只读探测：armd 激活且 7 电机全部 mode=0x15/fault=0/静止 → 原本正常。
    # 原本正常时跳过停止/重启，避免高位无谓重启（看门狗坠臂风险）。
    local daemon_json state_json
    [[ "$(service_state armd.service)" == "active" ]] || return 1
    if ! capture_cmd daemon_json daemon-status-probe env \
        PANTHERA_ENDPOINT=127.0.0.1:50051 \
        timeout "${CLI_TIMEOUT_S}s" \
        "$CLI_BIN" daemon status --json; then
        return 1
    fi
    json_arm_healthy "$daemon_json" || return 1
    if ! capture_cmd state_json state-probe env \
        PANTHERA_ENDPOINT=127.0.0.1:50051 \
        timeout "${CLI_TIMEOUT_S}s" \
        "$CLI_BIN" state get --json; then
        return 1
    fi
    json_state_healthy "$state_json"
}

repair_can() {
    check_arm_safety pre-stop-armd || return $?
    if arm_already_healthy; then
        log "repair=can result=already-healthy message=机械臂原本正常，无需重启（不需 sudo）"
        PHASE=verify
        verify_can
        return $?
    fi
    log "repair=can result=needs-restart message=电机不健康（0x0B/异常），执行停止→udev 刷新→重启"
    prepare_privilege
    stop_service armd.service 50051 || return $?
    refresh_udev || return $?
    start_service armd.service 50051 || return $?
    verify_can
}

repair_all() {
    # 停止顺序：armd -> camerad -> overhead；刷新后按相机 -> armd 启动。
    check_arm_safety pre-stop-armd || return $?
    prepare_privilege
    stop_service armd.service 50051 || return $?
    stop_service camerad.service 50052 || return $?
    stop_service overhead-camera.service 50053 || return $?
    refresh_udev || return $?
    start_service camerad.service 50052 || return $?
    start_service overhead-camera.service 50053 || return $?
    start_service armd.service 50051 || return $?
    verify_realsense || return $?
    verify_c920e || return $?
    verify_can
}

preflight_commands() {
    local command_name
    for command_name in systemctl udevadm ss pgrep readlink stat awk grep sed date mktemp python3 timeout flock; do
        need_command "$command_name"
    done
    [[ -x "$CLI_BIN" ]] || fail 3 "找不到可执行 CLI：$CLI_BIN；请先在 Pi 上完成 uv sync"
    if ((EUID != 0)); then
        need_command sudo
    fi
    if [[ "$TARGET" == c920e || "$TARGET" == all ]]; then
        need_command v4l2-ctl
    fi
    if [[ "$TARGET" == realsense || "$TARGET" == can || "$TARGET" == all ]]; then
        need_command lsusb
    fi
    systemctl --user show-environment >/dev/null 2>&1 || \
        fail 3 "当前终端没有可用的 systemd user session"
    [[ -f "$repo_root/pyproject.toml" ]] || fail 3 "不是 Panthera-WAM 仓库：$repo_root"
}

main() {
    parse_args "$@"
    init_log
    preflight_commands
    if ((DRY_RUN)); then
        log "lock=$LOCK_FILE result=skipped-dry-run"
    else
        exec 9>"$LOCK_FILE" 2>/dev/null || fail 3 "无法创建修复锁：$LOCK_FILE"
        flock -n 9 || fail 4 "已有另一个 repair-devices 实例运行：$LOCK_FILE"
        log "lock=$LOCK_FILE result=acquired"
    fi
    safety_preflight

    PHASE=plan
    log "plan=target:$TARGET stop_order=armd,camerad,overhead refresh=udev+device-map start_order=camerad,overhead,armd"
    if ((DRY_RUN)); then
        log "result=dry-run-complete rc=8"
        exit 8
    fi

    local rc=0
    case "$TARGET" in
        c920e) repair_c920e || rc=$? ;;
        realsense) repair_realsense || rc=$? ;;
        can) repair_can || rc=$? ;;
        all) repair_all || rc=$? ;;
    esac
    if ((rc != 0)); then
        fail "$rc" "目标 $TARGET 修复后未通过只读验收；日志：$LOG_PATH"
    fi
    PHASE=complete
    log "result=success rc=0 log=$LOG_PATH map=$MAP_PATH"
    if [[ "$TARGET" == can || "$TARGET" == all ]]; then
        printf '✅ 机械臂服务正常（armd active、7 电机 mode=0x15、fault=0）\n'
    else
        printf '✅ 设备修复完成，只读验收通过\n'
    fi
}

main "$@"
