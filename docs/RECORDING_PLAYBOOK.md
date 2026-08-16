# color-block 录制操作手册（真机 Pi 5）

> `color-block-000002`、`000003` 已用旧版定时采集流程打通。
> 从 `000004` 开始统一使用本手册的**定长双终端流程**。
> 固定契约：30 s 示范时长 → 901 个 canonical ticks → 900 个训练 frames。

## 0. 固定长度契约

录制脚本不再把 `--duration-s 30` 当作最终样本数，而是使用：

```text
--fixed-duration-s 30
```

含义：

- canonical 采样频率固定为 30 Hz；
- 30 s = 900 个时间间隔，因此原始对齐数据包含首尾两端，共 **901 ticks**；
- LeRobot packager 使用相邻样本生成 `q[t+1]` action，因此输出 **900 training frames**；
- collectord 默认额外采集 5 s 对齐余量，避免三路流启动/结束边界造成短一帧；
- 余量不是数据的一部分，最终 episode 仍严格为 901 ticks；
- 定长模式发布的是 **graceful stop 时刻的最后一个完整公共窗口**，不是采集开始后的第一个窗口；因此终端切换、建流和动作启动产生的前段空白不会进入最终 episode；
- `stop` 过早发送时，collectord 会继续采集，直到公共窗口至少达到目标 tick 数后再正常收尾；
- 不足固定 tick、出现丢帧或质量门失败时，保留 `FAILED.json`，不补帧、不复用帧、不发布 `COMPLETE`。

旧的 `000002`、`000003` 可能是 899 ticks，这是旧流程的历史数据；不要混淆为新定长契约。

## 0.5 preview-arm：等待式 preview 录制启动器（work-zero 方案）

`deploy/preview-arm.sh` 把「手工创建 FIFO + 后台 read + exec preview-record.sh」
封装为可重复脚本：

```bash
cd ~/Panthera-WAM
./deploy/preview-arm.sh color-block 021 --duration-s 30 --rate-hz 8
# 输出：
#   PREVIEW_ARMED
#   start_fifo=/tmp/panthera-preview-color-block-021-<pid>.start
#   log_path=/tmp/panthera-preview-color-block-021-<pid>.log
printf 'start\n' > /tmp/panthera-preview-color-block-021-<pid>.start
```

- `PREVIEW_ARMED` 只表示「等待中」，**不表示录制已开始、也不表示工作零位已就绪**；
- 收到 `start` 后才执行 `deploy/preview-record.sh`；`CAPTURE_STARTED` 仍由 recorder
  的 state+wrist+overhead 三路流质量门产生；
- 脚本不发送任何 arm 控制命令，**不是 zeroing 控制器**；录制前先把机械臂稳定在
  工作零位（正式流程为 `workzero gozero` 之后，见 `docs/JOINT_CONTROL.md` §7）；
- 已存在成功/失败目录时直接拒绝，绝不覆盖；recorder 失败时保留 /tmp 日志与
  唯一 FIFO/staging 线索；脚本从不主动 kill 正在收尾的 recorder；
- preview.json 增加 action-only 契约字段（见 §0.6）。

## 0.6 preview 的 action-only 契约字段

`preview.json` 在不破坏旧字段的前提下增加：

```json
{
  "motion_scope": "task_action_only",
  "work_zero_required": true,
  "gozero_excluded": true,
  "rezero_excluded": true,
  "capture_start_condition": "state+wrist+overhead",
  "action_window": {
    "start_sequence": 0,
    "end_sequence": 0,
    "start_monotonic_ns": 0,
    "end_monotonic_ns": 0
  }
}
```

- `action_window` 取录制轨迹 jsonl 的**真实首尾行**（首尾 sequence 与 Pi 单调时钟），
  空文件或损坏时缺省不填，绝不猜测或伪造边界；
- 旧 preview 没有这些字段时不被回填为成功，由后续 packager/validator 标记
  legacy/rejected（P4 落地）。

## 0.7 正式 episode 的 work-zero 会话顺序（P4，定死锁/阻尼锁语义）

术语：**定死锁** = MoveL 终止态（固件 PID 刚性保持，掰不动）；
**阻尼锁** = teach lock（HOLD，掰一下能复位）。

```bash
# 1. 进入工作零位：MoveL 回位 → 定死锁 + 已开爪（脚本开爪）
workzero gozero --confirm --wait
workzero show                       # 2. 只读确认（可选）
recordctl.sh start <episode>        # 3. 开始录制（此刻仍是定死锁）
teach start --manual-clutch          # 4. 切阻尼锁（录制/推理开始时显式切入）
teach clutch lock                    # 5. 阻尼锁锁定当前位置
teach clutch drag                    # 6. 恢复手拖，开始任务动作
# …… 手拖动/模型动作：把方块移到目标区域上方（动作指令到此为止）……
teach clutch lock --gripper 0.3      # 7. 目标位置闭爪 + 阻尼锁（脚本闭爪）
recordctl.sh stop <episode>          # 8. 录制结束 → fsync → 原子提交 → COMPLETE
recordctl.sh verify <episode>        # 9. 验收：901 ticks / 900 frames / rezero_allowed
workzero rezero --confirm --wait     # 10. 开爪（松方块，脚本）→ MoveL→工作0位 → 定死锁
```

- `recordctl.sh stop` 未完成采集/原子提交/质量门之前，**禁止** rezero；
  stop/verify 失败时 episode 标记 rejected，机械臂保持当前安全状态等待人工处理；
- 闭/开爪都由脚本完成，模型/示教动作只到「方块移到目标区上方」；
- episode.json 携带 `motion_scope=task_action_only`、`gozero_excluded/rezero_excluded`、
  `action_window` 与 `work_zero` 姿态（存在时），packager 只取 action 帧；
- 30 s → 901 canonical ticks → 900 training frames 契约不变。

## 1. 每次录制前检查

```bash
cd ~/Panthera-WAM
systemctl --user is-active armd.service camerad.service
pgrep -af 'overhead' | grep camerad        # C920e 通常是 nohup 进程，非 armd 服务
uv run --no-sync --package panthera-cli panthera camera status --source wrist --json
uv run --no-sync --package panthera-cli panthera camera status --source overhead --json
df -h /home/winbeau | tail -1
```

每段约 1.1 GB；定长录制还会先产生隐藏 staging 临时目录。不要在空间不足时开始。

若 teach 尚未运行：

```bash
cd ~/Panthera-WAM
./deploy/teach-cal.sh
```

看到 6 个关节 `mode=0x15`、`fault=0` 后再继续。修改参数或 armd 重启后必须重新执行此步骤。

## 2. 双终端布局

### 终端 A：录制控制面与日志

终端 A 会启动 collectord，并持续守着日志；它不是普通 `tail`，会在进程退出后自动检查
`COMPLETE`、固定 tick 数和质量门。

```bash
cd ~/Panthera-WAM
./deploy/recordctl.sh start color-block-000004
```

默认参数：

- `--fixed-duration-s 30`；
- `--fixed-margin-s 5`；
- 采集 depth；
- 日志和 PID 状态放在 `~/.cache/panthera-recordctl/color-block-000004/`；
- 脚本内部使用 `nohup`，SSH/TUI 断开不会杀掉 collectord。

如果终端 A 只想启动、不进入监看：

```bash
./deploy/recordctl.sh start color-block-000004 --detach
./deploy/recordctl.sh watch color-block-000004
```

### 终端 B：机械臂 lock / drag / stop

终端 B 只负责操作 teach，不要在这里启动第二个 collectord：

```bash
cd ~/Panthera-WAM
./deploy/teach-cal.sh lock   # 起始位形先锁住
```

确认终端 A 已打印 `collectord PID=...` 后，开始示教：

```bash
./deploy/teach-cal.sh drag   # 恢复手拖
```

30 秒目标动作内按需要循环：

```bash
./deploy/teach-cal.sh lock   # 放置/调整后锁住当前位置
./deploy/teach-cal.sh drag   # 拖到下一位置
```

建议动作顺序：起始位 → 拖到红块 → 拿起 → 平移 → 放到目标区 → 复位/停稳。动作幅度要足够，
否则相机时间偏置可能记录为 `insufficient_motion`（这不是质量门失败）。

## 3. 控制录制结束

动作完成后，先让机械臂处于安全的锁定姿态，再在终端 B 执行：

```bash
cd ~/Panthera-WAM
./deploy/teach-cal.sh lock
./deploy/recordctl.sh stop color-block-000004
```

`stop` 只向 collectord 发送 graceful finish 信号：

- 不停止 teach；
- 不停止 heartbeat；
- 不发送机械臂运动命令；
- 不直接 kill 转码/落盘过程；
- 如果过早执行，会继续等到至少 901 个公共 canonical tick 就绪，再截取**结束时最后 901 个 tick**收尾；
- 终端 A 会继续等待 PNG 转码、fsync、原子发布和验收。

**不要**使用 `timeout 100`、`kill -9`、`pkill collectord`。这些操作会打断 staging，留下
`.color-block-XXXXXX.tmp-*` 半成品。

如果本段明显无效、必须放弃：

```bash
./deploy/recordctl.sh abort color-block-000004
```

`abort` 发送 SIGTERM，保留 `FAILED.json` 供诊断；确认日志后才清理临时目录。

## 4. 终端 A 的完成结果

正常完成时日志最后一行类似：

```json
{"episode":"/home/winbeau/panthera-data/episodes/color-block-000004","status":"complete"}
```

脚本随后自动验收并要求：

```text
fixed.enabled = true
canonical_ticks = valid_ticks = 901
training_frames = 900
missing/duplicate/sequence_gaps/ring_overflows = 0
timestamp_regressions = 0
COMPLETE exists
```

也可以手动查看：

```bash
./deploy/recordctl.sh status color-block-000004
./deploy/recordctl.sh verify color-block-000004
```

如果只看原始文件：

```bash
EP=/home/winbeau/panthera-data/episodes/color-block-000004
ls "$EP"/COMPLETE
python3 - <<'PY'
import json
p="/home/winbeau/panthera-data/episodes/color-block-000004"
e=json.load(open(p+"/episode.json"))
s=json.load(open(p+"/sync_report.json"))
print("fixed", e["fixed_length"])
print("ticks", s["valid_ticks"], "/", s["canonical_ticks"])
print("depth", e["depth"])
print("offset", s["camera_state_offset"]["method"])
PY
```

## 5. 时间预算与临时目录

- 30 s 示范 + 默认 5 s 对齐余量；余量用于覆盖启动/结束边界，最终只保留停止时最后一个完整 30 s 窗口；
- 采集完成后还要进行 RGB/depth PNG 转换、Parquet 写入、fsync，通常总耗时 2–4 分钟；
- 录制期间 `episodes/` 下的 staging 目录以点开头：`.color-block-XXXXXX.tmp-*`，普通 `ls` 看不到；
- `recordctl` 终端 A 会一直等到最终 JSON 和 `COMPLETE`，期间不要杀；
- SSH 掉线不会改变 Pi 上的进程。恢复连接后执行：

```bash
cd ~/Panthera-WAM
./deploy/recordctl.sh status color-block-000004
./deploy/recordctl.sh watch color-block-000004
```

## 6. 当天收工

最后一段完成并确认机械臂由人扶稳后：

```bash
cd ~/Panthera-WAM
./deploy/teach-cal.sh stop
```

它会先进入约 10 s SAFE_HOLD；SAFE_HOLD 真机保持效果尚未完成现场确认，停止时务必扶住机械臂。
上传 HF 后再按需清理已确认的正式 episode：

```bash
rm -rf /home/winbeau/panthera-data/episodes/color-block-XXXXXX
```

只清理确定失败的临时目录：

```bash
rm -rf /home/winbeau/panthera-data/episodes/.color-block-*.tmp-*
```

## 7. 底层 collectord（仅调试）

正常采集不要手写长命令；如需诊断，可直接使用：

```bash
cd ~/Panthera-WAM
.venv/bin/collectord \
  --collection-root /home/winbeau/panthera-data \
  --episode-id color-block-000004 \
  --task 'Move the red block from the start area to the target area.' \
  --operator winbeau \
  --panthera-commit "$(git rev-parse HEAD)" \
  --calibration /home/winbeau/panthera-data/calibration.json \
  --identity /home/winbeau/panthera-data/identity.json \
  --fixed-duration-s 30 \
  --fixed-margin-s 5 \
  --capture-depth
```

底层进程支持 `SIGUSR1` graceful finish、`SIGTERM` abort；生产操作优先使用
`deploy/recordctl.sh`，因为它还负责 PID、日志、断线恢复和固定长度验收。
