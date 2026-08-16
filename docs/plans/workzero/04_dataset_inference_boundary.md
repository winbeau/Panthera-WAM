# P4：数据集与 DiT 推理的 action-only 边界

## 0. 目标

让数据采集和模型推理都明确知道：工作零位只是确定性的 session 前置/后置阶段，DiT 只负责任务动作，不学习回零。

## 1. 数据采集状态机

新增或固化一个高层 session 语义，实际可以先由 shell/CLI 编排，后续再服务化：

```text
IDLE
→ ZEROING
→ ZERO_STABLE
→ ACTION_RECORDING
→ ACTION_TERMINAL
→ ACTION_COMMITTED
→ RETURNING
→ IDLE
```

状态定义：

- `ZEROING`：`GoWorkZero` execution 运行，禁止记录训练 action；
- `ZERO_STABLE`：目标误差、速度、稳定时间均达到门；
- `ACTION_RECORDING`：collector/preview 正式写入训练数据；
- `ACTION_TERMINAL`：夹爪释放/任务完成/人工 stop；
- `ACTION_COMMITTED`：recordctl 已完成 fsync、原子提交、`COMPLETE` 和 verify；
- `RETURNING`：只有在 commit 成功后才允许 rezero。

任何异常终止都不能直接从 `ACTION_TERMINAL` 跳到 `RETURNING`，除非安全策略和操作员确认允许。

## 2. 正式 episode 的边界

### 2.1 推荐采集顺序

```bash
workzero gozero --confirm --wait
wait-workzero-stable
recordctl start <episode>
# 只执行任务动作
recordctl stop <episode>
recordctl verify <episode>
workzero rezero --confirm --wait
```

`recordctl stop` 完成前不能调用 rezero。若 stop/verify 失败：

-  episode 标为 rejected/incomplete；
- 不上传 HF；
- 不把失败回零片段拼回 action；
- 机械臂保持现有安全状态，等待人工处理。

### 2.2 采集窗口字段

在内部 manifest 或现有 metadata 中记录：

```json
{
  "motion_scope": "task_action_only",
  "work_zero_schema_version": 1,
  "work_zero_pose": {"joints": [], "gripper": 0.0},
  "zeroing_execution_id": "...",
  "action_start_sequence": 0,
  "action_end_sequence": 0,
  "returning_execution_id": "...",
  "excluded_phases": ["gozero", "rezero", "safe_hold", "startup"]
}
```

如果某个字段只能从日志猜测，宁可留空并拒绝正式发布，不要伪造。

### 2.3 901/900 契约

最终正式 episode 仍必须满足：

```text
30 s
→ 901 canonical ticks
→ 900 training frames
```

窗口裁剪必须发生在 canonical ticks 形成之前，或者由 packager 明确按 action window 裁剪后再重建 901/900。不能先把回零帧混入再靠模型训练脚本静默丢弃。

## 3. collectord/preview 接入策略

### 3.1 第一版优先使用物理边界

最小改动方案：

- gozero 完成且 settle 后才启动 collector；
- action 完成后先停止 collector；
- collector 完成提交后才 rezero。

这样 raw trajectory 本身就不含回零段。

### 3.2 防御性 manifest

即使采用物理边界，也在 preview/episode manifest 写入：

- `motion_scope=task_action_only`；
- `gozero_excluded=true`；
- `rezero_excluded=true`；
- work-zero pose/hash；
- action start/end sequence。

packager 验证这些字段与文件内容一致；不符合就 reject。

### 3.3 不复用失败数据

历史 preview/episode 若：

- 有 gozero/rezero 帧；
- 只有稀疏视频；
- 缺 state/torque/sequence；
- `COMPLETE` 缺失；

必须继续留在 `_rejected/`，不能因为新增 manifest 字段而重新认证。

## 4. DiT 训练输入

### 4.1 v1：只裁剪窗口，保持现有 action 数值格式

为了不同时改变数据边界和模型数值语义，第一版：

- 保留现有 observation/action schema；
- 让每个 episode 第一条 training frame 已经处于稳定工作零位；
- action label 只包含从零位开始的任务动作；
- 不把 `gozero/rezero` 当作 no-op 样本加入。

### 4.2 相对工作零位表示（后续兼容增量）

可选的后续变换：

```text
q_relative = q_absolute - q_workzero
```

推理时：

```text
q_absolute = q_workzero + q_relative
```

只有在确认 ActionStudent/DiT 当前 action codec 支持并有回归测试后才启用。第一版不要同时修改 action codec、归一化和数据边界。

### 4.3 元数据注入

模型或 dataloader 可读取：

- `work_zero_pose`；
- `motion_scope`；
- `action_window`；
- `coordinate_contract`；
- `stream_instance_id` 只作 provenance。

模型不需要看到 `gozero` 轨迹；元数据用于坐标解释和推理前置检查。

## 5. DiT 推理会话

新增一个高层 session helper（位置先查现有 ActionStudent/gateway 入口）：

```text
prepare:
  validate workzero
  acquire lease
  gozero --confirm
  wait stable

model_active:
  read observation
  infer DiT
  validate policy chunk
  apply policy chunk
  repeat until terminal

finish:
  stop policy stream
  if success and lease/EStop/status allow:
      rezero using same lease/session
  else:
      rezero=skipped + safe hold/stop
```

硬约束：

- `ZEROING` 阶段不能调用 `ApplyPolicyChunk`；
- `RETURNING` 阶段不能调用 `ApplyPolicyChunk`；
- preview/shadow 模式仍不能调用任何运动 RPC；
- finalizer 不得隐式 reacquire lease；
- policy session/request/observation sequence 继续使用现有单调校验；
- 模型 action 的终止必须先成为 terminal，再触发 rezero。

## 6. 失败语义

| 场景 | 是否自动 rezero | 处理 |
|---|---:|---|
| 模型正常完成且 lease 有效 | 是 | 停止模型、调用同一 session 的 rezero |
| 模型拒绝 action | 否 | 记录 reject，安全保持，人工检查 |
| lease 过期 | 否 | watchdog/cancel，不能重新 acquire |
| EStop | 否 | 保持 EStop 终态，人工复位后显式 gozero |
| collector stop/commit 失败 | 否 | episode reject，保存日志 |
| rezero 自身失败 | 不重试盲动 | 报告 execution/error，人工处理 |

## 7. 数据质量检查

增加或扩展 validator：

- `motion_scope == task_action_only`；
- action start 前没有训练帧；
- action end 后没有训练帧；
- 901/900 数量满足；
- sequence/timestamp 严格递增；
- 视频覆盖 action window；
- `gozero/rezero` excluded 字段存在且为 true；
- work-zero pose 7 维、有限、在限位内；
- 不允许把失败 execution 的帧上传。

## 8. P4 Gate

- [ ] 仿真 session 完整跑通 ZEROING→ACTION→COMMIT→RETURNING；
- [ ] mock gateway 断言 zeroing/returning 阶段 0 次 ApplyPolicyChunk；
- [ ] collector/preview manifest 能区分 action window；
- [ ] packager 只生成 action frames，901/900 通过；
- [ ] 正常、取消、EStop、lease loss、commit failure 的 rezero 语义均有测试；
- [ ] 第一版没有偷偷启用相对 action codec；
- [ ] 提交 `feat: 接入 action-only 数据与推理会话`。
