#!/usr/bin/env bash
# 等待式 preview 录制启动器：armed 后等待显式 `start` 信号，才启动 preview-record.sh。
#
# 设计契约（docs/plans/workzero/01_preview_arm_and_phase.md）：
#   - 本脚本只负责「等待式启动 + 退出码透传」，不发送任何 arm 控制命令；
#   - 不实现 gozero/rezero，不是 zeroing 控制器；
#   - 不覆盖任何历史成功目录；录制失败保留 /tmp 日志与唯一 FIFO/staging 线索；
#   - 成功与否由 preview-record.sh 的退出码与 preview.json 质量门判定，
#     不使用 tail|grep 文本作为成功依据；
#   - SIGINT/SIGTERM 只终止本脚本的等待流程，不向 armd 发送 stop/move，
#     也不强杀正在收尾的 recorder（等待其完成 fsync/原子提交）。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recorder="${PANTHERA_PREVIEW_RECORDER:-$repo_root/deploy/preview-record.sh}"

usage() {
    cat >&2 <<'EOF'
用法： ./deploy/preview-arm.sh TASK NUM [--duration-s 30] [--rate-hz 8] [--output-root DIR]

等待式 preview 录制启动器：
  1. 校验任务名/编号/时长/频率，拒绝空值、路径穿越与重复目标目录；
  2. 创建唯一 FIFO 与日志路径，输出 PREVIEW_ARMED；
  3. 等待一行显式 `start` 信号，收到后才执行 deploy/preview-record.sh；
  4. 完整继承 recorder 退出码，失败时保留日志。

环境变量（可选）：
  PANTHERA_PREVIEW_RECORDER        替换 recorder（默认 deploy/preview-record.sh）
  PANTHERA_PREVIEW_DURATION_S      默认 30
  PANTHERA_PREVIEW_RATE_HZ         默认 8
  PANTHERA_PREVIEW_OUTPUT_ROOT     默认 /home/winbeau/panthera-data/preview
  PANTHERA_PREVIEW_START_TIMEOUT_S 等待 start 的超时秒数，默认 300
EOF
}

die() {
    echo "preview-arm: $*" >&2
    exit 1
}

task=""
number=""
duration_s="${PANTHERA_PREVIEW_DURATION_S:-30}"
rate_hz="${PANTHERA_PREVIEW_RATE_HZ:-8}"
output_root="${PANTHERA_PREVIEW_OUTPUT_ROOT:-/home/winbeau/panthera-data/preview}"
start_timeout_s="${PANTHERA_PREVIEW_START_TIMEOUT_S:-300}"

while [[ $# -gt 0 ]]; do
    case "$1" in
    --duration-s)
        duration_s="${2:?--duration-s 需要数值}"
        shift 2
        ;;
    --rate-hz)
        rate_hz="${2:?--rate-hz 需要数值}"
        shift 2
        ;;
    --output-root)
        output_root="${2:?--output-root 需要目录}"
        shift 2
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    -*)
        usage
        exit 2
        ;;
    *)
        if [[ -z "$task" ]]; then
            task="$1"
        elif [[ -z "$number" ]]; then
            number="$1"
        else
            usage
            exit 2
        fi
        shift
        ;;
    esac
done

is_positive() {
    [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v v="$1" 'BEGIN { exit !(v > 0) }'
}

# ---- 缺参数：usage + 退出码 2 ----
if [[ -z "$task" || -z "$number" ]]; then
    usage
    exit 2
fi

# ---- 校验：空值、路径穿越、数值 ----
case "$task" in
    '' | */* | *..*) die "任务名不得为空或包含路径分隔符" ;;
esac
[[ "$number" =~ ^[0-9]{3}$ ]] || die "编号必须是三位数字，例如 001"
is_positive "$duration_s" || die "duration 必须是正数（秒）"
is_positive "$rate_hz" || die "rate 必须是正数（Hz）"
is_positive "$start_timeout_s" || die "start 超时必须为正数（秒）"

# ---- 重复目标目录拒绝：绝不覆盖历史成功/失败目录 ----
session="${task}_${number}"
target_dir="$output_root/$session"
if [[ -e "$target_dir" ]]; then
    die "目标目录已存在，拒绝覆盖：$target_dir"
fi

# ---- 唯一 FIFO / 日志（进程 PID 后缀，绝不复用旧路径）----
# 注意：必须用 O_RDWR（exec 3<>）打开 FIFO，否则 open() 本身会阻塞等待写者，
# 使 read -t 的超时永不生效；O_RDWR 打开立即成功，超时由 read 负责。
fifo="/tmp/panthera-preview-${task}-${number}-$$.start"
log="/tmp/panthera-preview-${task}-${number}-$$.log"
mkfifo "$fifo"
exec 3<>"$fifo"
cleanup() {
    exec 3>&- 2>/dev/null || true
    rm -f "$fifo"
}
trap cleanup EXIT

interrupted=0
on_signal() {
    interrupted=1
}
trap on_signal INT TERM

echo "PREVIEW_ARMED"
echo "start_fifo=$fifo"
echo "log_path=$log"
echo "recorder=$recorder"
echo "输出目录：$target_dir"
echo "机械臂稳定在工作零位后，执行： printf 'start\\n' > $fifo"

# ---- 等待显式 start 信号（只接受一行 `start`）----
if ! read -r -t "$start_timeout_s" signal <&3; then
    if [[ $interrupted -ne 0 ]]; then
        echo "PREVIEW_ABORTED interrupted" >&2
        exit 130
    fi
    echo "PREVIEW_ABORTED 等待 start 超时（${start_timeout_s}s）" >&2
    exit 3
fi
if [[ "$signal" != "start" ]]; then
    echo "PREVIEW_ABORTED 非法启动信号：${signal@Q}" >&2
    exit 4
fi
if [[ $interrupted -ne 0 ]]; then
    echo "PREVIEW_ABORTED interrupted" >&2
    exit 130
fi

# ---- 启动 recorder：透传退出码；从不主动 kill 正在收尾的 recorder ----
echo "PREVIEW_STARTING $recorder $task $number（日志：$log）" >&2
set +e
"$recorder" "$task" "$number" \
    --duration-s "$duration_s" \
    --rate-hz "$rate_hz" \
    --root "$output_root" 2>&1 | tee -a "$log"
status=$?
set -e
if [[ $status -ne 0 ]]; then
    echo "PREVIEW_FAILED recorder 退出码 $status（日志：$log）" >&2
else
    echo "PREVIEW_DONE 退出码 0（日志：$log）" >&2
fi
exit $status
