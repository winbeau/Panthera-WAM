# P1：等待式 preview-arm 与动作窗口边界

## 0. 目标

把当前需要手工创建 FIFO、后台 `read`、再 `exec preview-record.sh` 的流程封装为可重复脚本，并把“什么时候开始计训练 action”固定为明确的会话边界。

本阶段不实现机械臂回位，不调用 gozero/rezero，不改变模型控制。

## 1. 新增 `deploy/preview-arm.sh`

### 1.1 外部语义

建议接口：

```bash
./deploy/preview-arm.sh TASK NUM \
  [--duration-s 30] [--rate-hz 8] \
  [--output-root /home/winbeau/panthera-data/preview]
```

启动后立即输出：

```text
PREVIEW_ARMED
start_fifo=...
log_path=...
```

脚本等待一个明确的 `start` 信号，收到后执行现有 `deploy/preview-record.sh`。脚本本身不发送任何 arm 控制命令。

### 1.2 生命周期

1. 校验 task、编号、duration、rate；拒绝空值、路径穿越和重复目标目录。
2. 为本次调用创建唯一 FIFO 和日志路径，优先使用 `/tmp/panthera-preview-<task>-<num>-<pid>.start`。
3. 注册 `trap`：正常完成清理 FIFO；失败时保留日志和唯一 staging 线索，不删除历史失败数据。
4. 后台等待 FIFO 输入；只接受一行 `start`，其它内容退出并标记失败。
5. 启动 `preview-record.sh`，完整继承退出码。
6. 不使用 `tail|grep` 作为成功判据；由子进程退出码和 `preview.json` 最终质量门判定。
7. 输出 `CAPTURE_STARTED` 等待提示，但不把该提示当作质量成功。
8. 录制器失败时将目录移动到 `_rejected/<unique-name>` 或交给现有录制器处理，绝不覆盖既有目录。

### 1.3 并发和信号

- 同一 `<task,num>` 已存在成功目录时直接拒绝；历史 `_rejected` 允许同名前缀但必须追加唯一后缀。
- SIGINT/SIGTERM 只终止当前 preview worker，不向 armd 发送 stop/move。
- 不能在录制器尚未完成 fsync/原子提交时强制 kill；脚本应等待子进程退出。
- FIFO、log、staging 路径写入日志，便于下一对话诊断。

## 2. 动作窗口契约

### 2.1 preview

preview 录制的正确顺序：

```text
操作者先把机械臂带到工作零位并稳定
→ 启动 preview-arm（只 armed，不计时）
→ 触发 start
→ 等待 state+wrist+overhead 第一帧
→ 输出 CAPTURE_STARTED
→ 立即进入任务动作
→ 动作完成后 lock
→ preview worker 正常结束
→ 之后才允许 rezero
```

当前 `tools/preview-record.py` 已有三路第一帧门；本阶段只补脚本包装和 metadata，不回退该门。

### 2.2 正式 episode

正式数据的最小安全顺序：

```text
gozero 完成并稳定
→ recordctl start
→ 只执行任务动作
→ lock/释放动作结束
→ recordctl stop
→ 等待 COMPLETE 和 verify
→ rezero
```

如果 `recordctl stop` 失败、staging 未原子提交或质量门失败，禁止 rezero 自动触发；先保留机械臂当前安全状态并处理数据失败。

### 2.3 preview.json 增量字段

在不破坏旧字段的前提下增加：

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

字段只有在真实序列可确定时填值；不能用猜测或日志文本伪造边界。旧 preview 若没有字段，不回填为成功，只标记 legacy/rejected。

## 3. 实现步骤

### Step 1：先写脚本单测替身

准备 fake `preview-record.sh` 或可注入 recorder 命令，覆盖：

- armed 后不启动 recorder；
- 写入 `start` 才启动；
- recorder 退出码透传；
- 非 `start` 输入拒绝；
- FIFO 清理；
- 已存在成功目录不覆盖；
- recorder 失败不删除 log。

### Step 2：实现 Bash wrapper

保持 shell 逻辑短小：

- `set -euo pipefail`；
- 不嵌套不必要的 `bash -lc`；
- 所有临时路径变量经过 `mktemp`/安全命名；
- `wait` 真实等待子进程；
- 不依赖普通 `ls` 看 staging。

### Step 3：接入现有 preview recorder

只传递已支持的参数：

```text
color-block 001 --duration-s 30 --rate-hz 8
```

不要在 wrapper 中重新实现 camera/state gRPC，也不要添加第二套质量统计。

### Step 4：补文档

更新：

- `docs/RECORDING_PLAYBOOK.md`；
- `docs/FASTWAM_COLLECTION.md`；
- `deploy/README.md`；
- 总计划的 action window 表。

文档明确：preview-arm 是“等待式录制启动器”，不是 zeroing 控制器。

## 4. 检查

```bash
bash -n deploy/preview-arm.sh
git diff --check
```

用 fake recorder 做短时 smoke；不要在这一阶段启动真实 teach 或相机客户端竞争链路。

若需要运行现有 preview 3 秒自检，只能在确认 Pi 相机服务状态正常且没有其它相机客户端时执行；这仍不是 gozero 真机测试。

## 5. P1 Gate

- [ ] `PREVIEW_ARMED` 后 recorder 未启动；收到 `start` 后才启动。
- [ ] `CAPTURE_STARTED` 仍由三路 recorder 质量逻辑产生。
- [ ] 脚本不会覆盖成功或失败历史目录。
- [ ] preview metadata 标明 action-only、gozero/rezero excluded。
- [ ] recorder 未完成时不会被 wrapper 主动 kill。
- [ ] Bash smoke、现有 preview recorder 定向测试通过。
- [ ] 提交 `feat: 封装等待式 preview-arm 和动作窗口`。
