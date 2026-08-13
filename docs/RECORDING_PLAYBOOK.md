# color-block 录制操作手册（真机 Pi 5）

> 2026-08-13 首次完整打通验证：`color-block-000002`（30 s、899/899 tick 有效、
> 四流零丢帧/零溢出、深度完整、COMPLETE）。本手册流程与现场逐条验证一致，
> 照抄即可。背景契约见 `docs/FASTWAM_COLLECTION.md`（硬门/原子 staging）。

## 0. 每次录制前的三项检查（30 秒）

```bash
cd ~/Panthera-WAM
# 1) 服务在线
systemctl --user is-active armd.service camerad.service
pgrep -af 'overhead' | grep camerad        # overhead C920e 是 nohup 进程，非 systemd
# 2) 相机流（都要求 streaming:true、约 30 fps）
uv run --no-sync --package panthera-cli panthera camera status --source wrist --json
uv run --no-sync --package panthera-cli panthera camera status --source overhead --json
# 3) 磁盘余量（每段约 1.1 GB，录 10 段需 ~12 GB 空闲）
df -h /home/winbeau | tail -1
```

若 overhead 相机进程不在，重启它（固定用稳定别名，禁止写死 /dev/videoN）：

```bash
cd ~/Panthera-WAM && nohup uv run --no-sync --package panthera-armd camerad \
  --backend v4l2 --role overhead \
  --bind 100.78.118.74:50053 --local-bind 127.0.0.1:50053 \
  --device /home/winbeau/camera-devices/c920e \
  --width 1920 --height 1080 --fps 30 >/tmp/overhead-camerad.log 2>&1 &
```

## 1. 启动显式离合 teach（每次录制前）

```bash
cd ~/Panthera-WAM
./deploy/teach-cal.sh
```

脚本自动：杀旧 heartbeat → 重启 armd（解锁 0x0B）→ acquire → 后台 heartbeat →
`teach start --manual-clutch`。看到 6 关节 mode=0x15、fault=0 即就绪。

## 2. 录制一段 episode（约 3–5 分钟，中途不要杀）

```bash
cd ~/Panthera-WAM
EP=color-block-000003   # 每段自增；已用：000001（试录）、000002（首段正式）
COMMIT=$(git rev-parse HEAD)
nohup uv run --no-sync --package panthera-armd collectord \
  --collection-root /home/winbeau/panthera-data \
  --episode-id "$EP" \
  --task 'Move the red block from the start area to the target area.' \
  --operator winbeau \
  --panthera-commit "$COMMIT" \
  --calibration /home/winbeau/panthera-data/calibration.json \
  --identity /home/winbeau/panthera-data/identity.json \
  --capture-depth \
  --duration-s 30 > /tmp/collectord-$EP.log 2>&1 &
echo $! > /tmp/collectord-$EP.pid
```

**重要行为（实测教训）**：

- collectord **只在结束时打印一行 JSON**（`{"episode": "...", "status": "complete"}`），
  中途日志静默是正常的；约 30 s 采集 + 约 100 s PNG 转码/fsync，总耗时 2–4 分钟。
- 录制期间 `episodes/` 下出现的是**点前缀临时目录**（`.color-block-XXXXXX.tmp-*`，
  `ls` 不带 `-a` 看不见），完成后才原子改名为正式目录并写 `COMPLETE`。
- **不要用 100 秒以内的 timeout 包它**（实测 120s timeout 会把转码中的 episode 杀掉）；
  要限时就用 600 s。中途失败/被杀会残留 `.tmp-*` 目录，确认无用后 `rm -rf` 掉。

## 3. 录制期间的手臂操作（drag / lock）

- 采集前先把臂拖到起始位形，发一次 `lock` 稳定起始姿态；
- 开始采集（nohup 命令发出后）立即执行任务动作：**`drag` → 徒手拖动 → `lock`** 循环；
  每次放置/调整末端后 `lock`，拖到下一位置前 `drag`：

```bash
./deploy/teach-cal.sh drag   # 恢复手拖（kp 平滑降到 0，重力补偿仍在）
./deploy/teach-cal.sh lock   # 下一周期采样当前位置并锁定（kp=[4,20,20,2,2,1]）
```

- lock/drag 的完整状态机与参数见 `docs/JOINT_CONTROL.md` §6「Auto-Hold 状态机与显式离合」。
- **30 秒内尽量做出明确运动**（起步→拿起→平移→放下→复位）：过小运动会得到
  `offset_estimation_method=insufficient_motion`（不算失败，但相机/状态时间偏置无法估计）。

## 4. 等结果并验收（一条命令）

```bash
EP=color-block-000003
tail -3 /tmp/collectord-$EP.log      # 期待 {"episode": "...", "status": "complete"}
EP=/home/winbeau/panthera-data/episodes/$EP
ls "$EP"/COMPLETE && python3 - <<'PY'
import json
ep="/home/winbeau/panthera-data/episodes/color-block-000003"
d=json.load(open(ep+"/episode.json"))
s=json.load(open(ep+"/sync_report.json"))
print("success", d["success"], "| ticks", s["valid_ticks"], "/", s["canonical_ticks"],
      "| missing", s["missing_frames"], "| overflow", s["ring_overflows"])
print("offset_method", s["camera_state_offset"]["method"])
PY
```

验收标准：`success=True`；`valid_ticks == canonical_ticks`（30 s → 899–900）；
missing/duplicate/overflow/gap 全 0；`timestamp_regressions=0`。
`insufficient_motion` 只说明本段动作幅度不足以估计时间偏置，不是失败。

## 5. 结束与清理

- 当天录完最后一段：`./deploy/teach-cal.sh stop`（先 SAFE_HOLD 约 10 s，**务必扶住机械臂**），
  或 `pkill -f "control heartbeat"`（等价）。
- 上传 HF（每段录完就传，参照 `docs/FASTWAM_COLLECTION.md`）后清理本地：
  `rm -rf /home/winbeau/panthera-data/episodes/color-block-XXXXXX`。
- 失败残留清理：`rm -rf /home/winbeau/panthera-data/episodes/.color-block-*.tmp-*`。

## 6. 已确认的坑

| 坑 | 现象 | 处理 |
|---|---|---|
| timeout 太短 | `rc=124`、日志只有 uv warning、无 tmp 可见（其实有，点前缀） | 后台 nohup 运行，不要包短 timeout |
| 中途杀进程 | 残留 `.tmp-*` 半成品 | `rm -rf episodes/.color-block-*.tmp-*` |
| 低位/微动录制 | `insufficient_motion` | 不算失败；下一段加大动作幅度 |
| 高位 kill heartbeat | 失去重力前馈会坠落（SAFE_HOLD 真机保持未确认） | 停止时扶住机械臂；首选 `teach-cal.sh stop` |
