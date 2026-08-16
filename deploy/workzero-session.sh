#!/usr/bin/env bash
# work-zero 端到端会话引导：gozero → 录制 → 阻尼锁 → 手拖动作 → 闭爪 → stop/verify → rezero
#
# 使用： ./deploy/workzero-session.sh color-block-000010
#
# 术语（docs/FINAL_PLAN.md WZ-2）：
#   定死锁 = MoveL 终止态（固件 PID 刚性保持，掰不动）
#   阻尼锁 = teach lock（HOLD，掰一下能复位）
#
# 安全约定：
#   - 所有运动命令带 --confirm（服务端判定）；
#   - 每个阶段打印说明并等操作者回车后才进入下一阶段；
#   - 任何阶段失败立即停止（set -e），不自动重试；
#   - 收工释放：停 heartbeat 后臂会从定死锁进入柔顺，承重关节将下垂，请扶住。
set -Eeuo pipefail

cd ~/Panthera-WAM
export PANTHERA_ENDPOINT=127.0.0.1:50051
CLI=./.venv/bin/panthera
EPISODE="${1:?用法: ./deploy/workzero-session.sh <episode-id>（如 color-block-000010）}"

step() { printf '\n\033[1;36m========== %s ==========\033[0m\n' "$*"; }
confirm() { read -r -p ">>> 回车继续（Ctrl-C 退出）: " _; }

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
        echo "error: teach 启动失败" >&2
        return 1
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
                if grep -Eq '当前没有运行中的 teach' <<<"$lock_output"; then
                    "$CLI" teach start --manual-clutch 2>&1 | tail -1 || true
                fi
            fi
        elif lock_output=$("$CLI" teach clutch lock 2>&1); then
            printf '%s\n' "$lock_output"
            if grep -Eq 'state=hold' <<<"$lock_output"; then
                return 0
            fi
        else
            printf '%s\n' "$lock_output" >&2
            if grep -Eq '当前没有运行中的 teach' <<<"$lock_output"; then
                "$CLI" teach start --manual-clutch 2>&1 | tail -1 || true
            fi
        fi
        sleep 0.75
    done
    echo "error: teach 在 15s 内未进入可锁定状态；请检查 SAFE_HOLD、armd 日志和 E-stop" >&2
    return 1
}

# ---------------------------------------------------------------- 阶段 0
step "阶段 0：前置检查（只读，不运动）"
"$CLI" daemon status --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["sim"] is False and d["hardware_connected"] is True, "armd 异常"
print("armd OK（" + format(d["control_hz"], ".0f") + "Hz）")'
"$CLI" state get --json | python3 -c '
import json, sys
data = json.load(sys.stdin)
motors = data if isinstance(data, list) else list(data.get("joints", [])) + [data.get("gripper", {})]
bad = [m.get("name") for m in motors
       if not m.get("valid") or int(m.get("fault", 0)) != 0 or int(m.get("mode", -1)) == 0x0B]
assert len(motors) == 7 and not bad, f"电机异常: {bad}"
print("7 电机正常（valid/fault=0/mode≠0x0B）")'
"$CLI" control status --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["estop_engaged"] is False, "EStop 已触发，请先复位"
print("控制状态 OK（held=" + str(d["held"]) + "）")'
"$CLI" workzero show | head -4
confirm

# ---------------------------------------------------------------- 阶段 1
step "阶段 1：gozero（MoveL 回工作0位 → 定死锁 + 快速开爪）"
echo "⚠ 大位移回位：臂从当前位置移动到工作零位，请确认工作空间无障碍、人在场、E-stop 可触达"
confirm
"$CLI" control acquire --client-id session 2>/dev/null || echo "（已持有 lease，继续使用现有 token）"
if ! pgrep -f "control heartbeat" >/dev/null 2>&1; then
    (nohup "$CLI" control heartbeat >/dev/null 2>&1 & echo "heartbeat pid=$!")
fi
"$CLI" workzero gozero --confirm --wait
sleep 2
"$CLI" workzero show --json | python3 -c '
import json, subprocess, sys
cli = sys.argv[1]
show = json.load(sys.stdin)
state = json.loads(subprocess.run([cli, "state", "get", "--json"], capture_output=True, text=True).stdout)
motors = state if isinstance(state, list) else list(state.get("joints", [])) + [state.get("gripper", {})]
errs = [abs(m["position"] - t) for m, t in zip(motors, list(show["joints"]) + [show["gripper"]])]
print("回位误差:", [round(e, 4) for e in errs])
assert max(errs[:6]) <= 0.05, "关节回位误差过大"
assert errs[6] <= 0.1, "夹爪未开到工作零位姿态"
print("OK：定死锁保持 + 爪已开（可掰一下验证掰不动）")' "$CLI"
confirm

# ---------------------------------------------------------------- 阶段 2
step "阶段 2：启动录制（后台，只记录任务动作窗口）"
./deploy/recordctl.sh start "$EPISODE" --detach
sleep 3
./deploy/recordctl.sh status "$EPISODE"
confirm

# ---------------------------------------------------------------- 阶段 3
step "阶段 3：切阻尼锁（录制/动作开始时显式切入）"
teach_start_lock
confirm

# ---------------------------------------------------------------- 阶段 4
step "阶段 4：手拖任务动作（30 秒窗口内）"
cat <<'EOF'
动作流程（录制窗口内完成）：
  1) panthera teach clutch drag              # 恢复手拖
  2) 手拖到方块处
  3) panthera teach clutch lock --gripper 0.2   # 闭爪抓取 + 锁位
  4) panthera teach clutch drag              # 继续拖
  5) 把方块移动到目标区域上方
  6) panthera teach clutch lock --gripper 0.2   # 放置位闭爪保持 + 锁位
EOF
echo "现在机械臂在阻尼锁。按回车后我将发送 drag 开始动作窗口"
confirm
"$CLI" teach clutch drag
echo "拖动中……（30 秒内完成动作）完成后按回车：闭爪 + 阻尼锁"
confirm
"$CLI" teach clutch lock --gripper 0.2
confirm

# ---------------------------------------------------------------- 阶段 5
step "阶段 5：录制停止与验收（COMPLETE 后才能 rezero）"
./deploy/recordctl.sh stop "$EPISODE"
echo "等待 fsync/原子提交/COMPLETE……"
for _ in $(seq 1 60); do
    if ./deploy/recordctl.sh status "$EPISODE" 2>/dev/null | grep -q "published=COMPLETE"; then
        break
    fi
    sleep 5
done
./deploy/recordctl.sh verify "$EPISODE"
confirm

# ---------------------------------------------------------------- 阶段 6
step "阶段 6：rezero（开爪松方块 → MoveL 回工作0位 → 定死锁）"
echo "⚠ 回位运动：确认工作空间无障碍、人在场、E-stop 可触达"
confirm
"$CLI" workzero rezero --confirm --wait
confirm

# ---------------------------------------------------------------- 阶段 7
step "阶段 7：完成"
cat <<'EOF'
✔ 会话完成：臂在工作0位（定死锁 + 爪开），episode 已 COMPLETE。

继续下一段录制：直接再从「阶段 2」开始（无需重新 gozero）：
  ./deploy/recordctl.sh start <下一编号> --detach
  panthera teach start --manual-clutch && panthera teach clutch lock  # 首帧 HOLD，不自动 drag
  ……

收工（释放臂，请先扶住！）：
  pkill -f "control heartbeat"
  臂将从定死锁释放进入柔顺，承重关节（J2/J3/J4）会重力下垂——务必扶住。
EOF
