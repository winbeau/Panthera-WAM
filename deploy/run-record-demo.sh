#!/usr/bin/env bash
# run-record 批量演示：gozero → 逐条 run-record preview 轨迹 → rezero 收尾。
#
# 等价于（每条之间臂已回工作0位定死锁、夹爪开）：
#   gozero && rr 021 && rezero && rr 022 && rezero ...
#
# 用法：
#   ./deploy/run-record-demo.sh                    # 默认序列 021 022 023 024 025
#   ./deploy/run-record-demo.sh 021 023            # 指定编号序列
#   RUN_RECORD_DEMO_TASK=color-block ./deploy/run-record-demo.sh 021
#
# 注意：每条轨迹回放前需把方块摆到该轨迹的录制起始位；机械臂会自动运动，
# 人在场、工作空间无障碍、E-stop 可触达。任一步失败立即停止（fail-fast），
# 已完成的动作由 run-record 内部的自动 rezero 收尾。
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
CLI_SCRIPT="$repo_root/deploy/lerobot-collect.sh"
TASK="${RUN_RECORD_DEMO_TASK:-color-block}"

step() { printf '\n\033[1;36m========== %s ==========\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m⚠ %s\033[0m\n' "$*"; }

numbers=("$@")
if ((${#numbers[@]} == 0)); then
    numbers=(021 022 023 024 025)
fi
for number in "${numbers[@]}"; do
    [[ "$number" =~ ^[0-9]{3}$ ]] || {
        echo "error: 编号必须是三位数字，例如 021: $number" >&2
        exit 2
    }
done

warn "批量演示将自动回放 ${#numbers[@]} 条轨迹：确认人在场、E-stop 可触达"
step "gozero：回工作0位（定死锁 + 开爪）"
"$CLI_SCRIPT" gozero

for number in "${numbers[@]}"; do
    step "run-record ${TASK} ${number}"
    "$CLI_SCRIPT" run-record "$TASK" "$number"
    step "rezero 收尾（run-record 已自动回位，此处幂等确认）"
    "$CLI_SCRIPT" rezero
done

step "演示完成：臂在工作0位定死锁、夹爪开"
