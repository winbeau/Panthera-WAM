#!/usr/bin/env bash
# 安全回初始0位（URDF 零位，关节全 0 = 臂最低位形）。
#
# 用途：收工 / 重启 armd 前把臂放回低位，避免高位形失去保持后重力坠落
# （2026-08-16 事故：高位工作零位下重启 armd → 150ms 看门狗 → 阻尼 →
# 肘部重力坠落甩飞）。
#
# 方法：FK 计算关节全 0 的笛卡尔目标 → cartesian movel（真机已验证路径）
# → 到位后低位形重力稳定。
set -Eeuo pipefail

cd ~/Panthera-WAM
export PANTHERA_ENDPOINT=127.0.0.1:50051
CLI=./.venv/bin/panthera

step() { printf '\n\033[1;36m========== %s ==========\033[0m\n' "$*"; }
confirm() { read -r -p ">>> 回车继续（Ctrl-C 退出）: " _; }

step "前置检查（只读）"
"$CLI" state get --json | python3 -c '
import json, sys
data = json.load(sys.stdin)
motors = data if isinstance(data, list) else list(data.get("joints", [])) + [data.get("gripper", {})]
# 正常模式：0x15（阻尼）、0x90（POS-VEL 保持）、0xB0（MIT）等；0x0B 才是异常（堵转锁死）
bad = [m.get("name") for m in motors
       if not m.get("valid") or int(m.get("fault", 0)) != 0 or int(m.get("mode", -1)) == 0x0B]
assert len(motors) == 7 and not bad, f"电机异常: {bad}"
print("7 电机正常（valid/fault=0/mode≠0x0B）")'
"$CLI" control status --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["estop_engaged"] is False, "EStop 已触发，请先复位"
print("控制状态 OK")'

step "计算初始0位（关节全 0）笛卡尔目标"
TARGET=$(python3 - <<'PY'
import grpc
import numpy as np
from panthera_arm import arm_pb2, arm_pb2_grpc
from scipy.spatial.transform import Rotation

channel = grpc.insecure_channel("127.0.0.1:50051")
stub = arm_pb2_grpc.ArmServiceStub(channel)
resp = stub.GetForwardKinematics(
    arm_pb2.JointAnglesOptional(joint_angles=[0.0] * 6), timeout=10.0
)
channel.close()
pos = ",".join(f"{v:.6f}" for v in resp.position)
rpy = Rotation.from_matrix(np.array(resp.rotation_matrix).reshape(3, 3)).as_euler("xyz")
print(pos, ",".join(f"{v:.6f}" for v in rpy))
PY
)
POS=$(printf '%s' "$TARGET" | awk '{print $1}')
RPY=$(printf '%s' "$TARGET" | awk '{print $2}')
echo "目标（关节全 0）：pos=$POS rpy=$RPY"
confirm

step "MoveL 回初始0位（低位形，重力稳定）"
"$CLI" control acquire --client-id zero-home 2>/dev/null || echo "（已持有 lease）"
if ! pgrep -f "control heartbeat" >/dev/null 2>&1; then
    nohup "$CLI" control heartbeat >/dev/null 2>&1 &
    echo "heartbeat pid=$!"
fi
"$CLI" cartesian movel --pos "$POS" --rpy "$RPY" --wait

step "完成"
cat <<'EOF'
✔ 臂已回到初始0位（关节全 0，最低位形，重力稳定）。
  此后可以安全地：重启 armd / 收工（pkill -f "control heartbeat"）/ 关机。
  下次任务：先 workzero gozero 回到工作0位，再开始录制。
EOF
