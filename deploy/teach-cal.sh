#!/usr/bin/env bash
# 真机示教参数标定脚本（现场手调工具）。
#
# 每关节 5 个参数（顺序固定）：kp, kd, fc, fv, gravity_scale
# 可选连续重力残差通过 PANTHERA_TEACH_GRAVITY_RESIDUAL 在 armd.env 配置（Nm，6个值）。
#
# 用法（在 Pi 5 上）：
#   teach-cal.sh                        # 用当前基线拉起 teach
#   teach-cal.sh --J1 "0,0.1,-0.02,0,0" # J1 增量微调：kd+0.1、fc-0.02，其余 0 不变
#   teach-cal.sh --J3 "0,0,0,0,-0.05"     # J3 仅 scale-0.05
#   teach-cal.sh --show                 # 显示当前 6 关节参数
#   teach-cal.sh --reset                # 恢复出厂基线
#   teach-cal.sh lock                   # 运行中的 teach 锁定当前位置
#   teach-cal.sh drag                   # 运行中的 teach 恢复手拖
#   teach-cal.sh stop                   # 优雅停止（进入约10秒 SAFE_HOLD，请扶住机械臂）
#
# 增量规则：逗号分隔 5 个值（顺序 kp,kd,fc,fv,scale）；0 = 不修改；
# 数字 = 当前值 + 增量（可正可负）。scale 必须保持 >0。
#
# 脚本动作：停残留 heartbeat → 写 env(scale) → 重启 armd（解锁 0x0B）→
# acquire + 后台 heartbeat + teach start（kp/kd/fc/fv 从状态读取）。
# teach 保持运行直到 `pkill -f "control heartbeat"`（watchdog 随后自动停 teach）。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$HOME/.config/panthera-wam/armd.env"
state_file="$HOME/.config/panthera-wam/teach-cal.json"

if [[ ${1:-} == "lock" || ${1:-} == "drag" ]]; then
    [[ $# -eq 1 ]] || { echo "error: lock/drag 不接受额外参数" >&2; exit 2; }
    cd "$HOME/Panthera-WAM"
    uv run --no-sync --package panthera-cli panthera teach clutch "$1" 2>&1 \
        | grep -v "incompatible\|Warning" | tail -1
    exit ${PIPESTATUS[0]}
fi

if [[ ${1:-} == "stop" ]]; then
    [[ $# -eq 1 ]] || { echo "error: stop 不接受额外参数" >&2; exit 2; }
    cd "$HOME/Panthera-WAM"
    uv run --no-sync --package panthera-cli panthera teach stop 2>&1 \
        | grep -v "incompatible\|Warning" | tail -1
    echo "==> teach 已停止；将保持当前位置约 10s（SAFE_HOLD），请扶住机械臂"
    exit ${PIPESTATUS[0]}
fi

out="$(python3 - "$state_file" "$@" <<'PY'
import json
import os
import sys

state_file = sys.argv[1]
args = sys.argv[2:]
# 出厂基线（2026-08-12 现场标定定稿）：旋转轴稍带阻尼松手即停、承重轴防跳
# J1 肩旋 0.4 / J2 上臂 0.55 / J3 前臂 0.6 / J4 腕俯仰 0.4 / J5 腕滚 0.15 / J6 末滚 0.08
defaults = {
    "J1": [0, 0.4, 0.05, 0.02, 0.85],
    "J2": [0, 0.55, 0.15, 0.06, 0.85],
    "J3": [0, 0.6, 0.15, 0.06, 1.15],
    "J4": [0, 0.4, 0.15, 0.03, 1.0],
    "J5": [0, 0.15, 0.02, 0.01, 0.85],
    "J6": [0, 0.08, 0.02, 0.01, 0.85],
}
names = ("kp", "kd", "fc", "fv", "scale")
state = json.load(open(state_file)) if os.path.exists(state_file) else defaults

i = 0
while i < len(args):
    arg = args[i]
    if arg == "--reset":
        state = defaults
        i += 1
    elif arg == "--show":
        i += 1
    elif arg.startswith("--J") and len(arg) == 4 and arg[3] in "123456":
        joint = arg[2:]
        if i + 1 >= len(args):
            raise SystemExit(f"error: {arg} 需要 5 个增量值（kp,kd,fc,fv,scale，0 表示不修改）")
        deltas = [part.strip() for part in args[i + 1].split(",")]
        if len(deltas) != 5:
            raise SystemExit(f"error: {arg} 需要 5 个值，收到 {len(deltas)} 个")
        values = list(state[joint])
        for idx, delta in enumerate(deltas):
            try:
                values[idx] += float(delta)
            except ValueError:
                raise SystemExit(f"error: {arg} 第 {idx + 1} 个值非法: {delta!r}") from None
        if values[4] <= 0:
            raise SystemExit(f"error: {joint} scale 必须 > 0，当前 {values[4]}")
        if any(values[idx] < 0 for idx in range(4)):
            raise SystemExit(f"error: {joint} kp/kd/fc/fv 不得为负")
        state[joint] = values
        i += 2
    else:
        raise SystemExit(f"error: 未知参数 {arg!r}（支持 --J1..--J6 增量、--show、--reset）")

json.dump(state, open(state_file, "w"), indent=1)
print("STATE_JSON=" + json.dumps(state, separators=(",", ":")))
for joint in ("J1", "J2", "J3", "J4", "J5", "J6"):
    v = state[joint]
    print(f"{joint}: " + "  ".join(f"{n}={val:g}" for n, val in zip(names, v)))
PY
)"

echo "$out"
state_json="$(printf '%s\n' "$out" | sed -n 's/^STATE_JSON=//p')"
kp="$(printf '%s' "$state_json" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(",".join(str(s[f"J{i}"][0]) for i in range(1,7)))')"
kd="$(printf '%s' "$state_json" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(",".join(str(s[f"J{i}"][1]) for i in range(1,7)))')"
fc="$(printf '%s' "$state_json" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(",".join(str(s[f"J{i}"][2]) for i in range(1,7)))')"
fv="$(printf '%s' "$state_json" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(",".join(str(s[f"J{i}"][3]) for i in range(1,7)))')"
scale="$(printf '%s' "$state_json" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(",".join(str(s[f"J{i}"][4]) for i in range(1,7)))')"

echo "==> scale=$scale kp=$kp kd=$kd fc=$fc fv=$fv"
if grep -q '^PANTHERA_TEACH_GRAVITY_RESIDUAL=' "$env_file"; then
    echo "==> residual=$(sed -n 's/^PANTHERA_TEACH_GRAVITY_RESIDUAL=//p' "$env_file")"
fi
pkill -f "control heartbeat" 2>/dev/null || true
pkill -f "$repo_root/deploy/lease-heartbeat.py" 2>/dev/null || true
pkill -f "/tmp/panthera-lease-heartbeat.py" 2>/dev/null || true
if grep -q '^PANTHERA_TEACH_GRAVITY_SCALE=' "$env_file"; then
    sed -i "s|^PANTHERA_TEACH_GRAVITY_SCALE=.*|PANTHERA_TEACH_GRAVITY_SCALE=$scale|" "$env_file"
else
    echo "PANTHERA_TEACH_GRAVITY_SCALE=$scale" >> "$env_file"
fi
echo "==> env 已更新，重启 armd（解锁并加载新系数）..."
systemctl --user restart armd.service
sleep 8
systemctl --user is-active armd.service >/dev/null || { echo "armd 未激活" >&2; exit 1; }

cd "$repo_root"
hb_log="${PANTHERA_TEACH_HEARTBEAT_LOG:-/tmp/panthera-lease-heartbeat.log}"
nohup "$repo_root/.venv/bin/python" "$repo_root/deploy/lease-heartbeat.py" >"$hb_log" 2>&1 &
sleep 1
"$repo_root/.venv/bin/panthera" teach start --manual-clutch --kp "$kp" --kd "$kd" --fc "$fc" --fv "$fv" 2>&1 | grep -v "incompatible\|Warning" | tail -1
timeout 20 "$repo_root/.venv/bin/panthera" state get 2>&1 | grep -v "incompatible\|Warning" | sed -n '4,9p'
echo "==> teach 运行中（显式离合）：./deploy/teach-cal.sh lock 锁定；./deploy/teach-cal.sh drag 手拖"
echo "==> heartbeat 日志：$hb_log"
echo "==> 停止（会先 SAFE_HOLD 约10秒）：./deploy/teach-cal.sh stop 或 pkill -f \"$repo_root/deploy/lease-heartbeat.py\""
