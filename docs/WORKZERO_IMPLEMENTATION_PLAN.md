# Panthera-WAM 工作零位与任务动作边界实现总计划

> 版本：v1.0
> 编写基线：`fba5a7a`（Pi 侧 preview 三路流就绪后再开始计时）
> 目标：在新的对话中按本计划完成实现，不让 DiT 学习 `gozero/rezero`，并把所有真机运动限制在可审计、可取消、可急停的服务端连续流式控制内。
> 状态：计划已冻结，代码尚未开始本计划新增的 work-zero 实现。

## 1. 一句话目标

把一次任务会话拆成三个严格隔离的阶段：

```text
PREPARE_ZERO   -- 非模型、服务端确定性 gozero、等待稳定
TASK_ACTION    -- 只有这一段允许 DiT 或人工示教产生 action
RETURN_ZERO    -- 非模型、服务端确定性 rezero
```

训练数据和模型 action 只覆盖 `TASK_ACTION`。`gozero`、`rezero`、启动准备、失败后的安全收尾都不能进入训练 action。

## 2. 已冻结的架构决策

### 2.1 工作零位是应用层姿态，不是硬件 encoder zero

- `setzero` 保存当前 6 个关节加夹爪的 7 轴**工作姿态**。
- 不调用 SDK `set_reset_zero()`，不改变电机坐标参考、软限位解释或出厂零点。
- 命令树使用 `workzero setzero/gozero/rezero`，与既有 `calibrate zero` 明确分开。
- 默认文件：`~/.config/panthera-wam/work-zero.json`。
- 支持环境变量 `PANTHERA_WORK_ZERO_PATH` 覆盖路径。
- 文件由 armd 所在主机持有；客户端不直接读写 Pi 上的工作零位文件。

### 2.2 `setzero` 必须基于 teach clutch lock 的同一状态样本

- 要求 active teach、显式 manual clutch、当前控制权 lease 有效。
- 服务端请求 `LOCK`，等待 teach 控制循环确认 lock generation。
- 在处理 lock 的同一个 7 电机状态样本中捕获 6 个关节和夹爪。
- 不采用“先发送 lock，再分别调用 GetJointState/GetGripperState 拼接”的竞态方案。
- 保存前再次做有限性、连接完整性和软限位校验。
- `drag`、teach stop、后续示教不会修改已持久化工作零位。

### 2.3 `gozero/rezero` 不复用危险路径

真机 work-zero 回位不得调用：

```text
move
movej
JointPositionMotion
单帧 position_frame
TeachPlayback 的 posvel 起点移动
RunJointTrajectory / CartesianTrajectoryMotion 作为第一版真机回位路径
```

第一版采用新的服务端 `WorkZeroMotion`：

- 由 `HardwareLoop` 每控制周期推进；
- 连续输出 MIT/POS-VEL-TQE-KP-KD 完整帧；
- 目标、速度、加速度、力矩和软限位均由服务端计算和检查；
- 客户端只提交目标和观察 execution，不自行循环发电机帧；
- `EStop`、lease/watchdog、取消减速、execution registry 继续由现有安全层统一管理；
- 实现中严禁调用 `position_frame()` helper 作为单帧目标命令。

### 2.4 armd 启动不自动运动

“启动一次任务时先到工作零位”应实现为显式会话命令，而不是 systemd 启动 hook：

```text
armd/systemd 启动：只初始化服务，不发送运动
workzero gozero --confirm：操作员在场后才允许运动
```

断电恢复、服务重启、网络重连都不能导致机械臂自动移动。

### 2.5 训练/推理边界

训练和推理统一采用：

```text
go zero → settle → 开始记录/开始模型控制
动作完成 → 停止记录/停止模型控制 → 再回 zero
```

- `recordctl` 在 gozero 完成并稳定后才开始正式窗口。
- `recordctl stop` 完成采集、转码、fsync、原子提交和 `COMPLETE` 后才允许 rezero。
- preview 的视频、raw trajectory、replay trajectory 都只描述 task action 窗口。
- 如未来保留会话级全量日志，必须在 manifest 中标明 `excluded_ranges`，packager 只取 action window。
- 第一版不改变既有 LeRobot action 数值表示，先做窗口边界隔离；相对工作零位 action 作为兼容性验证后的独立增量。

## 3. 交付物索引

详细步骤拆分在以下文件，按编号执行：

1. [`plans/workzero/00_preflight_contract.md`](plans/workzero/00_preflight_contract.md) — 前置核实、权威文档、基线和停止门。
2. [`plans/workzero/01_preview_arm_and_phase.md`](plans/workzero/01_preview_arm_and_phase.md) — 等待式 `preview-arm`、动作窗口和采集编排。
3. [`plans/workzero/02_store_setzero_proto_cli.md`](plans/workzero/02_store_setzero_proto_cli.md) — 持久化模型、lock snapshot、proto、Python/C# 生成和 CLI。
4. [`plans/workzero/03_streaming_workzero_motion.md`](plans/workzero/03_streaming_workzero_motion.md) — 服务端连续流式 `WorkZeroMotion`、execution、取消和安全边界。
5. [`plans/workzero/04_dataset_inference_boundary.md`](plans/workzero/04_dataset_inference_boundary.md) — LeRobot/collectord/action window/DiT 推理会话。
6. [`plans/workzero/05_verification_and_hardware_rollout.md`](plans/workzero/05_verification_and_hardware_rollout.md) — 单元测试、仿真、集成、分阶段真机和最终验收。

新对话首先读取本文件和上述 6 个子计划，再从尚未完成的第一个 gate 继续。

## 4. 阶段总览与依赖

| 阶段 | 名称 | 主要结果 | 依赖 | 真机 |
|---|---|---|---|---|
| P0 | 前置契约核实 | FINAL_PLAN/CLI/安全事实重新核实并回写 | 无 | 否 |
| P1 | preview 与窗口边界 | `preview-arm.sh`、action-only manifest、无覆盖提交 | P0 | 否 |
| P2 | work-zero 存储与 setzero | 原子持久化、lock generation、读写 RPC/CLI | P0 | 仿真；最后一项真机 |
| P3 | WorkZeroMotion | 服务端连续流式 gozero、execution、cancel | P0、P2 | 仿真 |
| P4 | session 编排与数据集 | gozero/record/action/stop/rezero 串联；训练只取 action | P1、P2、P3 | 仿真 |
| P5 | 全量验证 | 单测、gRPC、CLI、packager、回归检查 | P1–P4 | 否 |
| P6 | 真机分级验收 | 只在所有软件 gate 通过后进行 | P5 | 是 |
| P7 | 文档与交接 | 手册、运行记录、commit、下一对话入口 | P6 | 否 |

任何阶段的 gate 未通过，都不能跳到下一阶段；真机阶段不能用“代码测试通过”替代。

## 5. 建议的最终 API 表面

### 5.1 proto

第一版尽量减少重复契约：

```text
GetWorkZero        // 只读，无 lease
SetWorkZero        // 需 lease + active manual-clutch teach
GoWorkZero         // 需 lease + confirm，提交服务端 WorkZeroMotion
```

`rezero` 不复制一份运动实现：

- CLI `workzero rezero` 调用同一个 `GoWorkZero` RPC，传递 `reason=post_action` 或等价 operation label；
- action/session wrapper 也只调用同一 RPC；
- 如果后续需要服务端事务语义，再单独增加 `Rezero` RPC，不在第一版预先复制。

建议消息包含：

```text
WorkZeroPose
  schema_version
  joints[6]
  gripper_position
  captured_at_ms
  sampled_monotonic_ns（若可得）
  state_sequence（若可得）
  stream_instance_id
  source

GetWorkZeroResponse
  exists
  pose
  reject_reason

SetWorkZeroRequest
  optional confirm（如当前 CLI 安全规范要求）
SetWorkZeroResponse
  accepted
  saved
  pose
  lock_generation
  reject_reason

GoWorkZeroRequest
  confirm
  wait
  optional timeout_s
  operation_name/reason
GoWorkZeroResponse 或 ExecutionAccepted
  accepted
  execution_id
  reject_reason
```

具体字段编号以现有 `arm.proto` 保留号审计为准，禁止复用已经发布的 field number。

### 5.2 CLI

规范命令：

```bash
panthera workzero show
panthera workzero setzero
panthera workzero gozero --confirm --wait
panthera workzero rezero --confirm --wait
```

可选部署快捷入口：

```bash
./deploy/workzero.sh show
./deploy/workzero.sh setzero
./deploy/workzero.sh gozero --confirm --wait
./deploy/workzero.sh rezero --confirm --wait
```

快捷脚本只能调用 CLI/RPC，不能直接接触 SDK 或旁路写电机。

### 5.3 会话语义

```text
workzero gozero --confirm --wait
wait stable
preview-arm / recordctl start
MODEL_ACTIVE 或人工示教
recordctl stop / action terminal
workzero rezero --confirm --wait
```

失败/取消/E-stop/lease loss 时：

```text
不自动重新 acquire lease
不盲目 rezero
保留安全停止或 SAFE_HOLD
报告 rezero=skipped 及原因
```

## 6. 关键状态机

### 6.1 WorkZeroMotion

```text
VALIDATE
  → CAPTURE_CURRENT
  → PLAN_STREAM
  → RUNNING
  → SETTLE
  → DONE

任意阶段：
  EStop       → HardwareLoop EStop 路径
  lease loss  → request_cancel(WATCHDOG)
  client stop → controlled deceleration
  invalid state/limit/timeout → FAILED + safe finish
```

必须记录：

- target pose；
- current pose；
- signed error；
- per-joint velocity/acceleration/torque maxima；
- fraction；
- final error；
- cancel reason；
- whether any frame was emitted after terminal state（必须为否）。

### 6.2 数据会话

```text
IDLE
  → ZEROING（非模型）
  → ZERO_STABLE
  → ACTION_RECORDING / MODEL_ACTIVE
  → ACTION_TERMINAL
  → ACTION_COMMITTED
  → RETURNING（非模型）
  → IDLE
```

`ACTION_RECORDING/MODEL_ACTIVE` 是唯一允许生成训练 action 的状态。

## 7. 文件变更预览

实现前以实际仓库为准，但预期涉及：

```text
proto/arm.proto
proto/gen.sh（若需补检查或生成目标）
proto/gen/python/panthera_arm/*（重新生成，不手改）
armd/src/armd/workzero.py                 新增
armd/src/armd/motion.py                   WorkZeroMotion + lock snapshot 接口
armd/src/armd/hardware_loop.py            必要的状态/执行桥接
armd/src/armd/grpc_service.py             RPC、权限、execution、状态门
armd/src/armd/execution.py                若需扩展 operation metadata
armd/src/armd/backend/*                   仅在现有限位/帧抽象不足时聚焦修改
cli/src/panthera_cli/__main__.py          workzero 命令
cli/src/panthera_cli/client.py             复用 lease/heartbeat/wait helper
cli/tests/*                                CLI smoke
armd/tests/*                               motion/store/gRPC/safety

deploy/preview-arm.sh                      新增

deploy/workzero.sh                         可选薄封装

tools/preview-record.py                    action window/manifest 字段

deploy/recordctl.sh                        stop/commit 与 rezero 顺序门

tools/lerobot-v3/src/...                  只在窗口裁剪需要时修改

docs/FINAL_PLAN.md                         回写前置核实和新增决策

docs/JOINT_CONTROL.md                      增加 WorkZero 真机规则

docs/RECORDING_PLAYBOOK.md                 增加采集顺序

docs/FASTWAM_COLLECTION.md                 增加 action-only 契约
```

禁止修改或复制 `vendor/Panthera-HT_SDK` 源码。若确有 SDK 变更需求，另开 public fork 变更并更新 gitlink；第一版应避免此路径。

## 8. 提交边界

每一个里程碑一个独立 commit，提交信息使用中文前缀：

1. `docs: 冻结 work-zero 实现契约与验证门`
2. `feat: 封装等待式 preview-arm 和动作窗口`
3. `feat: 增加工作零位持久化与 setzero 契约`
4. `feat: 增加服务端连续流式 WorkZeroMotion`
5. `feat: 接入 action-only 数据与推理会话`
6. `test: 完成 work-zero 仿真和 gRPC 安全回归`
7. `docs: 补充工作零位真机操作手册`

每个 commit 前运行该阶段最小检查；不要把未验证的真机实验结果混入软件实现 commit。

## 9. 总验收条件

只有全部条件满足，才可称为完成：

- [ ] `setzero` 不调用 `set_reset_zero`，能从同一 lock sample 保存 7 轴姿态。
- [ ] 文件原子写、损坏拒绝、权限 `0600`、重启可加载。
- [ ] `gozero/rezero` 不调用 `move/movej/JointPositionMotion/单帧 position_frame`。
- [ ] WorkZeroMotion 由 HardwareLoop 每周期运行，目标、软限位、力矩、速度、取消均有服务端门。
- [ ] E-stop、lease/watchdog、force acquire、CancelExecution 都能终止或安全收尾，不能自动恢复运动。
- [ ] preview-arm 等待三路第一帧后才开始计时，并且不覆盖失败历史目录。
- [ ] 正式采集在 gozero 稳定后才开始，在 rezero 前完成 `COMPLETE` 提交。
- [ ] 训练 manifest/packager 明确 action window，gozero/rezero 不进入 900 training frames。
- [ ] DiT 推理会话在 ZEROING/RETURNING 阶段绝不调用模型 action apply。
- [ ] Python stub 重新生成并通过导入；C# 由现有 `Grpc.Tools` 从同一 `arm.proto` 重新生成并完成 build。
- [ ] 仿真测试和 gRPC/CLI 回归通过。
- [ ] 真机测试每一步均有用户当次确认、保守力矩限制、E-stop 可触达，且完成分阶段记录。
- [ ] `FINAL_PLAN.md` 已回写实现前核实结论和 work-zero 新增风险，不与 AGENTS/JOINT_CONTROL 冲突。

## 10. 新对话启动提示词

复制以下内容作为新对话第一条消息：

```text
继续实现 Panthera-WAM 工作零位方案。先读取：
1. docs/WORKZERO_IMPLEMENTATION_PLAN.md
2. docs/plans/workzero/00_preflight_contract.md
3. docs/plans/workzero/01_preview_arm_and_phase.md
4. docs/plans/workzero/02_store_setzero_proto_cli.md
5. docs/plans/workzero/03_streaming_workzero_motion.md
6. docs/plans/workzero/04_dataset_inference_boundary.md
7. docs/plans/workzero/05_verification_and_hardware_rollout.md
以及 AGENTS.md、docs/FINAL_PLAN.md、docs/JOINT_CONTROL.md。

严格遵守：
- gozero/rezero 只能使用服务端连续流式控制；禁止 move/movej/单帧 position_frame；
- setzero 必须继承 teach clutch lock，并从同一 7 轴状态样本持久化；
- DiT/训练 action 只覆盖工作零位稳定后的任务动作，不包含 gozero/rezero；
- 真机运动前必须当次确认、打印动作、二次确认，优先仿真；
- 改 proto 后重新生成 Python stub，C# 由同一 proto 重新 build；
- 不修改 vendor/Panthera-HT_SDK；
- 先完成当前未通过的阶段 gate，再进入下一阶段。

先报告当前 git/status、计划完成到哪个 P、最近一次检查结果，然后从第一个未完成步骤开始实现；不要直接动真机。
```
