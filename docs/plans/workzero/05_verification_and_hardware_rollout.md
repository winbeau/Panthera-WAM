# P5/P6/P7：验证、仿真、分阶段真机与交接

## 0. 总原则

代码测试通过不等于真机已验证。所有真机动作都必须：

- 用户当次明确在场并授权；
- 脚本先打印将执行的动作和参数；
- 二次确认后才发送运动；
- E-stop 可立即触达；
- 使用保守默认力矩/速度；
- C# 前端和其它 D405 客户端关闭；
- 记录完整日志、execution_id、commit、状态和失败原因。

不在本计划中请求或记录密码、token、私钥。

## 1. P5 软件验证顺序

### 1.1 静态和生成检查

```bash
cd /home/winbeau/Papers/ICLR2027-WAM-Reprojection/Panthera-WAM
./proto/gen.sh
# 检查 generated diff 只来自 proto 变更
git diff --check

cd armd
uv run ruff check src tests
uv run pytest -q
uv run python -m compileall -q src tests

cd ../cli
uv run ruff check src tests
uv run pytest -q

cd ..
bash -n deploy/preview-arm.sh
git diff --check
```

如果仓库有 package-specific required checks，按各自 pyproject 追加；不把未改变路径的长套件反复重跑。

### 1.2 Store/teach/motion 分层

先跑最小改动路径：

1. WorkZeroStore；
2. Teach lock snapshot；
3. WorkZeroMotion spy backend；
4. HardwareLoop execution；
5. gRPC permission/lease；
6. CLI smoke；
7. dataset window/packager。

某一层失败时先修根因并重跑该层，再进入下一层。

### 1.3 禁止路径审计

用代码搜索和测试双重证明：

```bash
rg -n "position_frame\(|JointPositionMotion|MoveJ|movej|RunJointTrajectory|CartesianTrajectoryMotion" \
  armd/src/armd/workzero.py armd/src/armd/motion.py armd/src/armd/grpc_service.py
```

搜索结果中允许出现：

- 文档/拒绝原因/测试断言；
- 其它既有功能代码。

但 WorkZeroMotion 的执行函数不得调用这些路径。增加一个 spy backend/monkeypatch 测试，在运行 gozero 时若调用 `position_frame` 立即失败。

## 2. P5 仿真集成场景

启动 `armd --sim`，通过真实 gRPC/CLI 走以下场景：

### 场景 A：保存工作零位

```text
AcquireControl
→ TeachStart(manual_clutch=true)
→ SetWorkZero
→ GetWorkZero
→ TeachClutch(drag)
→ TeachStop
```

检查：

- pose 有 7 个有限值；
- 文件可加载；
- drag/stop 不改变文件；
- 没有调用硬件 SetZero。

### 场景 B：正常 gozero

```text
AcquireControl + heartbeat
→ GoWorkZero(confirm)
→ StreamExecution
→ DONE
→ state error within tolerance
```

检查 fraction 单调、execution terminal 后无额外帧。

### 场景 C：取消/lease/EStop

分别测试：

- CancelExecution；
- ReleaseControl；
- heartbeat 停止导致 watchdog；
- force acquire；
- EStop；
- ClearEStop 不自动 resume。

每种场景都必须看到受控终态，而不是无限运行或自动重新开始。

### 场景 D：数据边界

mock 一个 action：

```text
GoWorkZero
→ stable
→ start collector
→ action frames
→ stop/COMPLETE
→ GoWorkZero(reason=post_action)
```

检查正式 frame 列表没有 zeroing/returning frame，901/900 契约通过。

### 场景 E：模型边界

mock DiT/gateway，记录每个 RPC：

- zeroing 期间 ApplyPolicyChunk 次数为 0；
- action 期间只出现允许的 policy calls；
- returning 期间 ApplyPolicyChunk 次数为 0；
- lease loss 时 finalizer 不 reacquire。

## 3. P6 真机前置检查（不运动）

在 Pi 的持久 tmux SSH 会话中复用一个终端，先只读检查：

- `armd.service` active；
- `camerad` wrist/overhead active；
- `overhead-camera.service` active；
- 7 个 motor valid、fault=0、mode 正常；
- EStop 状态符合预期；
- `work-zero.json` 路径和权限；
- C# viewer 和其它相机客户端关闭；
- D405 使用稳定 alias/SDK serial；
- 网络/lease endpoint 正确；
- 当前工作树和部署 commit 与测试记录一致。

只读失败就停止，不执行 gozero。

## 4. P6 真机分级顺序

### H0：服务与读状态

- 不发送任何写帧；
- 读取 daemon/control/robot state；
- 确认 7 轴在线、无故障、软限位配置正确。

### H1：setzero 锁存

用户在场扶持机械臂：

1. 启动显式 manual-clutch teach；
2. 操作者把机械臂放在已确认安全的工作姿态；
3. 先打印即将锁定的 7 轴值和文件路径；
4. 用户二次确认；
5. 执行 `workzero setzero`；
6. 检查 lock generation、HOLD、文件原子写和 `0600`；
7. 只读复查姿态，不启动回位。

H1 失败不得继续 H2。

### H2：零距离/仿真对应验证

若当前策略允许零残差测试：

- 目标与当前 pose 相同；
- 只验证 RPC 门、execution、settle 和无意运动；
- 如果 P3 对小残差定义为拒绝，则验证明确 reject，不强行让真机走低速补偿。

### H3：受控大于安全最小位移的回位

仅在 H0–H2 通过、用户再次明确确认后：

1. 选择可见、无障碍、机械臂旁可扶持的安全位形；
2. 用已批准的流式方式将机械臂带离工作零位，不能使用 move/movej；
3. 停止并确认状态稳定；
4. 打印 gozero 将执行的 target、duration、limits、execution policy；
5. 用户二次确认；
6. 执行 `gozero --confirm --wait`；
7. 观察每个关节、夹爪、速度、力矩、mode、fraction；
8. 到位后确认稳定时间和误差；
9. 若出现异常 mode、力矩突变、限位接近、噪声或人体风险，立即 EStop 并停止该阶段。

不要为了测试而把目标设为低速 jog 死区；WorkZeroMotion 的 MIT 行为必须按 P3 真实参数执行。

### H4：取消/lease/EStop 演练

每一项都单独获得当次确认：

- 正常 CancelExecution；
- heartbeat 终止/watchdog；
- force acquire；
- EStop；
- clear EStop 后确认不会自动 resume。

任何一次失败都回到软件仿真诊断，不能连续重复真机尝试。

### H5：一次真实 action session

只有 H3/H4 全通过后：

```text
gozero
→ settle
→ preview/record start
→ 人工动作或 DiT 最小闭环
→ action stop/COMPLETE
→ rezero
```

第一条只做一个短、可观察、夹爪开合明确的任务。不要直接批量 002–010。

## 5. 真机停止条件

立即停止当前阶段并记录日志：

- 任一 motor mode 进入异常/0x0B；
- EStop 不在规定窗口内生效；
- lease/watchdog 行为不符合预期；
- 软限位/力矩/速度超限；
- 目标方向反转或出现不可解释的振荡；
- cycle overrun 影响控制周期；
- gripper 速度/位置/力矩门报错；
- 相机客户端竞争导致 wrist depth clock-fit 异常；
- preview state/video 质量门失败；
- collectord 未 `COMPLETE` 就试图回位；
- 操作者无法持续扶持或触达 EStop。

停止后：

1. 不自动重试；
2. 保存 execution/log/state；
3. 视情况 EStop/保持；
4. 只读确认硬件状态；
5. 在新对话中先诊断，不直接发下一条运动命令。

## 6. P7 文档与最终交接

更新：

- `docs/WORKZERO_IMPLEMENTATION_PLAN.md`：勾选完成项、记录 commit；
- `docs/FINAL_PLAN.md`：架构和风险结论；
- `docs/JOINT_CONTROL.md`：真机操作和禁止路径；
- `docs/RECORDING_PLAYBOOK.md`：gozero/record/action/stop/rezero；
- `docs/FASTWAM_COLLECTION.md`：action-only 数据契约；
- `deploy/README.md`：CLI/脚本示例和失败处理。

最终记录：

```text
软件 commit
proto 生成状态
仿真测试命令/结果
真机日期、操作者确认、execution_id
work-zero pose 的 schema/hash（不泄露秘密）
正式 episode/preview 的 action window
未完成风险和下一步
```

## 7. 完成判定

P5/P6/P7 全部通过才允许：

- 把首条真实 episode 上传 HF；
- 固定 revision；
- 在 H100 做 ActionStudent 冒烟；
- 批量制作 preview/formal 002–010。

即使 H1–H4 通过，也不能把 gozero/rezero 帧加入训练集。
