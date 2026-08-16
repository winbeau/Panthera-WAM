# P3：服务端连续流式 WorkZeroMotion

## 0. 目标

实现 `gozero/rezero` 的实际运动状态机。所有目标生成、帧发送、限位、取消、EStop 交互都在 armd/HardwareLoop 内完成。

本阶段先仿真，未通过全部 sim/gRPC gate 前禁止真机运动。

## 1. 运动模型选择

### 1.1 第一版只做关节空间

工作零位由关节姿态定义，因此第一版不引入 Cartesian waypoint、IK 或 pinocchio 规划：

- 避免 IK 分支和不可达目标问题；
- 避免把 CartesianTrajectoryMotion/POS-VEL 轨迹误当作当前固件安全回位路径；
- 保持目标和软限位都在 6 轴关节空间直接可审计。

可选 FK 诊断只读，不参与控制闭环。

### 1.2 连续帧类型

`WorkZeroMotion.step()` 每个 HardwareLoop 周期发送完整 `JointFrame`：

```text
mode = POS_VEL_TQE_KP_KD
arm_position = 当前时间的平滑目标
arm_velocity = 平滑目标速度/零速收尾
arm_torque = 受 tau_limit 限幅的重力/摩擦补偿或安全前馈
arm_kp/kd = 服务端保守配置
gripper = 同一完整帧中的受限目标/保持策略
```

严禁：

- `position_frame()`；
- 一次性 POS-VEL target；
- `JointPositionMotion`；
- SDK `moveJ/moveL/iswait=True`；
- 客户端通过 `JointMIT` 自己驱动一个“伪 gozero”。

WorkZeroMotion 是服务端的确定性 motion，不是模型策略。

## 2. WorkZeroMotion 数据和状态

建议在 `armd/src/armd/motion.py` 新增类，或拆到 `workzero_motion.py` 后在 motion 中导出：

```text
目标：WorkZeroPose
超时：bounded timeout
阶段：VALIDATE/CAPTURE/RUN/SETTLE/CANCEL/DONE/FAILED
fraction：单调 [0,1]
errors：6 轴 + gripper
reject_reason
cancel_reason
max_observed_velocity/acceleration/torque
```

构造时复制 ndarray，拒绝非有限值、错误维度、负增益、负 timeout。

## 3. 启动前检查

`GoWorkZero` RPC 在提交 motion 前必须完成：

1. `confirm=true`；
2. lease 有效并 heartbeat 一次；
3. work-zero 文件存在且 schema/数值有效；
4. 当前 7 轴 state 全部 valid；
5. EStop 未 engaged；
6. 没有其它 active motion；
7. teach、policy chunk、trajectory、recording 的互斥状态满足要求；
8. 目标在当前 backend soft limits 内；
9. 目标 gripper 在 gripper position/torque limit 内；
10. 默认超时和控制参数是保守值，不能由任意客户端绕过服务端上限。

如果当前 residual 低于计划定义的最小安全范围，必须按 P0 冻结的策略处理：

- 要么进入已验证的 MIT hold/settle；
- 要么显式拒绝 `WORK_ZERO_RESIDUAL_TOO_SMALL`，不偷偷退回单帧位置模式。

## 4. 轨迹生成

### 4.1 平滑关节路径

对每个 arm joint 使用起点到目标的有界时间函数（例如五次 smoothstep）：

```text
q(t)      = q0 + s(t) * (q1-q0)
q_dot(t)  = s_dot(t) * (q1-q0)
q_ddot(t)  = s_ddot(t) * (q1-q0)
```

生成后逐周期裁剪/拒绝：

- position 不越 soft limit，保留 margin；
- velocity 不超过 backend limits；
- acceleration 不超过 backend limits；
- torque 不超过配置和请求上限；
- signed error 方向不能反转导致超调。

如果时间函数要求的速度超过限制，应延长 duration 或拒绝，不把速度硬裁剪成无法按时到达的隐式行为。

### 4.2 速度死区和小残差

当前固件对低速 jog 有堵转锁死历史，但该问题不能简单外推到 MIT。第一版必须通过仿真和一次受控真机 spike 验证：

- 大位移连续 MIT 是否稳定；
- 接近目标时低速/零速 MIT hold 是否稳定；
- 夹爪目标是否需要独立分阶段；
- 不允许用低速 `joint jog` 作为偷偷的最终补偿。

没有真机证据前，允许在 feature gate 下只开放仿真 `WorkZeroMotion`，真机 RPC 返回明确 disabled reason。

## 5. gripper 策略

工作零位 pose 保存 gripper，但必须明确回位策略：

### 默认建议

- `setzero` 记录 gripper；
- `gozero/rezero` 以普通 gripper limits 约束目标；
- 在完整 MIT frame 中使用受限 gripper target/velocity/torque；
- 不复用 TeachPlayback 的“关闭 gripper velocity limit”开关；
- 普通 GripperMIT/GripperVelocity 限制保持不变。

如果仿真/真机验证证明夹爪和 arm 同时回位不安全，则第一版明确拆成：

```text
arm WorkZeroMotion
→ arm settle
→ 独立、受限的 gripper return/open motion
```

但仍不能使用单帧危险位置路径，也不能把夹爪速度门全局关闭。

## 6. 取消、lease 和 EStop

### 6.1 客户端取消

`CancelExecution` 调用 `request_cancel(CLIENT)`。后续每周期：

1. 读取当前状态；
2. 将目标速度按固定周期递减；
3. 继续发送完整 MIT 安全帧；
4. 速度归零后进入已定义的 hold/idle damping；
5. 返回 `CANCELLED`，不标记 DONE。

取消过程不得调用 `position_frame()`，不得直接硬切到一个未验证目标。

### 6.2 lease/watchdog

- lease 过期由现有控制层请求 cancel；
- cancel 超时沿现有 `_cancel_active_motion_and_wait` 逻辑升级到安全停止/EStop；
- 旧客户端不能在 lease 失效后继续发帧；
- force acquire 取消旧 WorkZeroMotion；
- ReleaseControl 先等待安全收尾，再进入 idle damping。

### 6.3 EStop

HardwareLoop 周期顺序不改：EStop 优先于 motion step。EStop 后：

- 不自动恢复 WorkZeroMotion；
- execution 进入失败/取消的结构化终态；
- ClearEStop 不自动 resume；
- 操作员必须重新显式 gozero。

## 7. execution/gRPC

`GoWorkZero` 采用现有长动作模式：

```text
unary GoWorkZero → execution_id
StreamExecution → fraction/state/error
CancelExecution → controlled cancellation
```

扩展 `ExecutionStatus` 或 `ExecutionRecord` 时：

- 不破坏 moveL/teach play；
- operation name 只用于日志和诊断；
- `fraction` 单调；
- terminal 后不再发送控制帧；
- `StreamExecution` 能区分 DONE/FAILED/CANCELLED。

`wait=true` 由 CLI 观察，不让 gRPC handler 阻塞 HardwareLoop。

## 8. 服务端 feature gate

建议增加配置：

```text
PANTHERA_WORKZERO_ENABLED=0/1
PANTHERA_WORKZERO_REAL_HARDWARE_ENABLED=0/1
```

默认策略：

- sim 可在测试中启用；
- real hardware 第一版默认关闭；
- 只有 P5 软件 gate 通过、P6 现场逐级确认后才开启；
- 配置改变需要服务重启，但重启本身不得自动运动。

不要用 `PANTHERA_ALLOW_UNVERIFIED_TEACH` 代替 WorkZero 独立门控。

## 9. 测试清单

### 运动单测

- 从不同起点到目标，signed error 正确；
- 轨迹时间单调；
- position/velocity/acceleration/torque 均不越限；
- 每步都写 MIT frame；
- spy backend 断言未调用 `position_frame`；
- 目标相同的策略明确且可重复；
- cancel 在固定周期内减速；
- timeout 进入 FAILED 和安全状态；
- invalid state 立即失败，不写危险帧；
- gripper 位置/速度/力矩门仍生效；
- terminal 后 step 不再发送帧。

### HardwareLoop 单测

- active motion 互斥；
- EStop 优先；
- watchdog/force acquire/release 触发 cancel；
- completion future 状态正确；
- 200Hz 周期无长阻塞；
- cycle overrun 有记录。

### gRPC 单测

- 无 lease/无 confirm/无 pose/有 active motion/EStop 时拒绝；
- feature gate 关闭时拒绝 real；
- `GoWorkZero` 返回 execution_id；
- `StreamExecution` 终态正确；
- `CancelExecution` 不会重新启动；
- `rezero` reason 只影响 metadata，不复制另一套运动实现。

## 10. P3 Gate

- [ ] sim 中完整执行 gozero、settle、cancel、timeout、EStop；
- [ ] spy backend 证明没有 `position_frame`/MoveJ/MoveL/JointPositionMotion；
- [ ] execution registry 和 CLI wait 通过；
- [ ] gripper 限制没有全局放宽；
- [ ] real hardware feature gate 默认关闭；
- [ ] 200Hz/周期统计没有新增长阻塞；
- [ ] 提交 `feat: 增加服务端连续流式 WorkZeroMotion`。
