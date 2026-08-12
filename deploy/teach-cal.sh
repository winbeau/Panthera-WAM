#!/usr/bin/env bash
# 真机示教重力补偿逐关节标定脚本（现场手调工具）。
#
# 用法（在 Pi 5 上，任意目录）：
#   ~/Panthera-WAM/deploy/teach-cal.sh 0.85 0.85 1.0 0.7 0.85 0.85   # J1..J6 六个标定系数
#   ~/Panthera-WAM/deploy/teach-cal.sh "0.85,0.85,1.0,0.7,0.85,0.85"  # 或逗号分隔
#   ~/Panthera-WAM/deploy/teach-cal.sh 0.85 0.85 1.0 0.7 0.85 0.85 \
#       --fc "0.05,0.15,0.15,0.15,0.02,0.02" --fv "0.02,0.06,0.06,0.03,0.01,0.01"
#   ~/Panthera-WAM/deploy/teach-cal.sh                               # 沿用 env 中现有值
#
# 脚本动作：停残留 heartbeat → 写入 env → 重启 armd（解锁 0x0B）→
# acquire + 后台 heartbeat + teach start（零刚度，补偿激活）。
# teach 保持运行直到 `pkill -f "control heartbeat"`（watchdog 随后自动停 teach）。
set -euo pipefail

env_file="$HOME/.config/panthera-wam/armd.env"
fc_arg=""
fv_arg=""
scale_args=()

while [ $# -gt 0 ]; do
    case "$1" in
        --fc)
            fc_arg="--fc $2"; shift 2 ;;
        --fv)
            fv_arg="--fv $2"; shift 2 ;;
        *)
            scale_args+=("$1"); shift ;;
    esac
done

if [ ${#scale_args[@]} -eq 1 ] && [[ "${scale_args[0]}" == *,* ]]; then
    scale="${scale_args[0]}"
elif [ ${#scale_args[@]} -eq 6 ]; then
    scale="$(IFS=,; echo "${scale_args[*]}")"
else
    scale="$(grep -oP '(?<=^PANTHERA_TEACH_GRAVITY_SCALE=).*' "$env_file" 2>/dev/null || echo '0.85,0.85,1.0,0.7,0.85,0.85')"
fi

echo "==> scale = $scale"
pkill -f "control heartbeat" 2>/dev/null || true
if grep -q '^PANTHERA_TEACH_GRAVITY_SCALE=' "$env_file"; then
    sed -i "s|^PANTHERA_TEACH_GRAVITY_SCALE=.*|PANTHERA_TEACH_GRAVITY_SCALE=$scale|" "$env_file"
else
    echo "PANTHERA_TEACH_GRAVITY_SCALE=$scale" >> "$env_file"
fi
echo "==> env 已更新，重启 armd（解锁并加载新系数）..."
systemctl --user restart armd.service
sleep 8
systemctl --user is-active armd.service >/dev/null || { echo "armd 未激活" >&2; exit 1; }

cd "$HOME/Panthera-WAM"
uv run --no-sync --package panthera-cli panthera control acquire --client-id teach-cal 2>&1 | grep -v "incompatible\|Warning" | tail -1
nohup uv run --no-sync --package panthera-cli panthera control heartbeat >/tmp/hb.log 2>&1 &
sleep 4
uv run --no-sync --package panthera-cli panthera teach start $fc_arg $fv_arg 2>&1 | grep -v "incompatible\|Warning" | tail -1
timeout 20 uv run --no-sync --package panthera-cli panthera state get 2>&1 | grep -v "incompatible\|Warning" | sed -n '4,9p'
echo "==> teach 运行中（scale=$scale）。拖动测试后如需停止：pkill -f \"control heartbeat\""
