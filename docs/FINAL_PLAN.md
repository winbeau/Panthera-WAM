# Panthera-WAM 总计划（敲定稿）

> 状态：规划阶段定稿，供审阅。本文档不含实现代码。
> 本稿在 `CLI_PLAN.md` / WPF 三份视觉稿基础上，逐条回填了对抗审计提出的 16 项 load-bearing 缺陷（见文末「审计修订对照」），并将 CLI+armd 契约与 WPF 设计合并成一份。
> 覆盖口径：SDK（`Panthera` 38 + `Recorder` 4 = 42 个公开方法/静态方法）每一项，落到 **(a) CLI 命令 + arm.proto rpc** / **(b) 显式不暴露+理由** / **(c) v2 占位** 三种归宿之一，逐行可核对（Part 1 §5）。

---

## 总览与架构回顾

```
Windows: WPF 控制终端 (Fluent, 三主题) ──┐
WSL2:    panthera-cli (typer)  ─────────┤→ gRPC (localhost:50051) → armd 守护 → 官方 SDK(零修改*) → usbipd → Panthera-HT
未来:    WAM 训练/推理 ──────────────────┘                                              └ RealSense D405 (v2)
```

- **硬件**：Panthera-HT 六轴机械臂（高擎，7×USB 串口）+ Intel RealSense D405，单臂。WSL2 经 usbipd 独占硬件。
- **armd**：WSL2 内常驻守护，独占硬件，对外只暴露 gRPC+protobuf。安全层 = 控制权互斥 / watchdog 心跳 / 软限位预检 / EStop。
- **客户端**：`panthera-cli`（Python typer）+ WPF 控制终端（.NET 9，Fluent，系统/浅色/深色三主题）。两者都是纯 gRPC 客户端，不直接碰硬件。
- **零修改\* 的边界**：官方 SDK 源码零修改，但 armd **不能**直接调用 SDK 的阻塞式方法（`iswait=True` 等待、`moveL()`、`Recorder.play()`）——这些方法会把唯一的硬件线程钉死数秒到整条轨迹，破坏可抢占安全层。armd 改为**用 SDK 的公开规划/控制原语（`compute_cartesian_path` / `septic_interpolation` / `Joint_Pos_Vel(iswait=False)` / `check_position_reached`）在自己的控制循环里逐周期步进**。M0-2 真机结果进一步否决了 SDK moveL 内部的 MIT 执行路径，详见 §V9。

### 三条贯穿全局的架构决策（审计核心矛盾的收敛结论）

1. **单线程 HardwareLoop 只做非阻塞步进**：所有触碰电机的 SDK 调用串行化到 HardwareLoop；任何一次调用都不得长阻塞。等待/轨迹/回放都拆成「每个控制周期推进一步 + 每周期先查 estop/cancel 标志」的状态机。→ 使 watchdog 不饥饿、EStop 可在数个周期内抢占、StreamState 缓存始终新鲜。
2. **控制权走 gRPC metadata 统一拦截**：`lease_token` 不塞进每条消息，而是放在 gRPC header（`x-panthera-lease`），armd 服务端拦截器对「写/动机械臂类」RPC 统一校验持锁，对「只读」RPC 一律放行。→ 契约层有牙齿，且监控面板无需持锁即可旁观。
3. **纯计算与电机 I/O 分流**：只读运动学/动力学（FK/IK/Jacobian/动力学/路径规划）只用 pinocchio 模型、不碰电机，走独立计算 worker（专用第二个 pinocchio 实例）+ 墙钟超时预算，绝不阻塞控制循环；凡是要真实读写 USB 的调用（`get_current_*`、`check_position_reached`、所有运动）一律 marshal 回 HardwareLoop。

---

# Part 1 — CLI + armd 无损控制计划（v1 / v2）

## 1. armd 线程与执行模型（契约级前置，回填审计 G1/G2/G8）

### 1.1 HardwareLoop 单周期时序

HardwareLoop 是唯一持有 `Panthera` 对象、独占 7×USB 的线程，以固定 **200Hz** 周期运行（M0-1 真机锁定，见 §V9）。**每个周期严格按序**：

1. **查 estop 标志**：置位则本周期立即执行「按当前控制模式的停止动作」（§4 停止策略表），并冻结后续步进。
2. **查 cancel 标志**：命中则对在飞 execution 触发安全收尾（减速停止）。
3. **刷新状态缓存**：`get_current_state`（含夹爪），供 StreamState / 到达判定使用。
4. **推进活动运动一步**：见 §1.2 的三类运动状态机（非阻塞）。
5. **喂 watchdog / 记录**：更新心跳时钟；若录制开启则调 `Recorder.log`。

**硬约束**：HardwareLoop 内**永不**调用 `iswait=True` 的等待、`moveL()`、`Recorder.play()` 这类单体阻塞方法。等待必须「在循环里推进」，而不是「把循环钉死」。

### 1.2 运动状态机（非阻塞步进，取代直接调阻塞方法）

| 运动 | 初稿做法（已废弃） | 定稿做法 |
|---|---|---|
| `joint move --wait` / `movej --wait`（Mode A） | `Joint_Pos_Vel(iswait=True)` 阻塞轮询到 15s | 下发一次 `Joint_Pos_Vel(iswait=False)`；随后每周期 `check_position_reached` 判到达/超时，并检查 estop/cancel；asyncio 侧 `await` 一个「到达事件」 |
| `cartesian movel`（Mode B） | 直接调 `moveL()` 单体阻塞 | armd 自建执行循环：`compute_cartesian_path → compute_time_parameterization → smooth_trajectory_spline` 得到逐点轨迹，HardwareLoop 每周期发一个点（`Joint_Pos_Vel(iswait=False)`），每点发布 `fraction`、每点查 cancel/estop；末点继续保位收敛。**不调 `moveL()`** |
| `teach play`（Mode B） | 直接调 `Recorder.play()` 静态阻塞 | armd 复现 `play` 的取帧/滤波/逐点下发循环（起点移动 + 逐帧回放），每帧发布 `fraction`、每帧查 cancel/estop。**不调 `Recorder.play()`** |

> **零修改边界更新**：`_execute_trajectory`（moveL 内部执行循环）、`_prepare_playback_frames` / `_moving_average`（play 内部预处理）是 SDK 私有方法。armd 不 import 私有名；规划与预处理逻辑按公开原语等价重写。moveL 的**执行原语不照抄** `_execute_trajectory` 的 MIT 分支——M0-2 真机证明其在当前固件上无法可靠跟踪，改用 POS-VEL。此类组合能力单列于 §4b。

### 1.3 EStop 抢占的物理可行性（回填 G2）

EStop 之所以能「<100ms 生效」，正是因为 §1.1 保证了 HardwareLoop 永不长阻塞：asyncio 层收到 `EStop` 只置一个 estop 标志，HardwareLoop 在**下一个控制周期开头**（§1.1 步骤 1）就读到并执行 `set_stop()`。M0-1 真机实测标志到 `set_stop()` 返回 **7.73ms**。**不采用**从 asyncio 线程直接触碰 USB 的旁路写通道（那会破坏单线程独占、与循环 I/O 打架）。

### 1.4 纯计算分流与 IK 让路（回填 G8）

- **电机 I/O 类调用**（`get_current_*`、`check_position_reached`、所有运动下发）：一律 marshal 到 HardwareLoop 队列串行执行，无并发。
- **纯计算类调用**（FK/IK/Jacobian/可操作度/动力学/`compute_cartesian_path` 预览）：只依赖 pinocchio 模型、不碰电机，放在独立计算 worker，用**专用的第二个 pinocchio 模型实例**（启动时用同一 URDF 独立构造），与 HardwareLoop 使用的模型物理隔离。
- **计算 worker 必须是独立进程，不能是线程**（M0-3 实测结论，见 §V8）：Python GIL 使线程内的 IK 与控制循环争用解释器——500Hz 下线程方案把周期 p95 从 2.00ms 抬到 3.43ms、最坏 **13.22ms（≈6.6 个周期）**；换成独立进程后 p95/max 与无并发基线**完全一致**（2.00/2.05ms），且 IK 吞吐反而高 2.6 倍。故 §1.4 的「独立计算 worker」**明确定义为独立进程**（`multiprocessing`，子进程自建模型实例——双实例隔离由进程边界天然保证）。
- **IK 让路**：`inverse_kinematics(multi_init=True)` 给**墙钟超时预算**，超时返回 `found=false, timeout=true`。**默认值由 5s 下调为 0.5s**——M0-3 实测不可达目标（跑满 `num_attempts×max_iter` 的最坏路径）仅 ~170ms，可达目标 p50 仅 2.2ms；0.5s 已含 3 倍余量，且能让不可达目标快 10 倍地失败返回。
- ~~待核实~~ → **已结案**：pinocchio 模型可安全独立双实例化（M0-3 Q1 通过），无需退化为「marshal 回 HardwareLoop」方案。

---

## 2. 阻塞语义 → gRPC 五种模式（修订表）

| 模式 | 适用 | gRPC 形状 | 定稿说明 |
|---|---|---|---|
| **A｜服务端有界等待** | `joint move --wait` / `movej --wait` | 普通 unary，服务端等到达/超时才返回 | 服务端**不调 `iswait=True`**；用 §1.2 的「非阻塞下发 + 每周期 check_reached」推进等待。gRPC deadline 由 CLI 按 `timeout_s + 2s` 逐调用设置（§6） |
| **B｜提交即返回 + 执行流 + 可取消** | `moveL`、`teach play`、多点轨迹 | unary 返回 `execution_id` → `StreamExecution` 推 `fraction/state` → `CancelExecution` 中止 | 服务端**不调 `moveL()`/`play()`**，用 §1.2 自建逐点执行循环产出 `fraction`、承接取消 |
| **C｜双向流（持续意图）＋ 新鲜度窗口** | `joint jog`、`cartesian jog`、`joint mit` | `stream Command → stream Feedback` | **新鲜度窗口是唯一权威停止机制**：超过窗口（关节 jog 250ms / 笛卡尔 jog 建议更小如 120ms，可配）未收到新指令，服务端把速度/力矩前馈归零。松键/断连/客户端崩溃**全部**靠它兜底 |
| **D｜纯计算只读** | FK/IK/Jacobian/可操作度/动力学/plan-preview | 普通 unary，立即返回 | 走 §1.4 计算 worker，不阻塞控制循环；IK client deadline 建议 5–10s |
| **E｜服务端内部自驱动** | `Recorder.log` | RPC 只开关标志位，实际调用在 HardwareLoop 内 | 避免录制逐帧搬上网络 |

---

## 3. 控制权与权限模型（回填 G4/G10/G11）

### 3.1 lease 走 metadata 统一拦截

`AcquireControl` 返回 `lease_token`；此后客户端把它放进 gRPC metadata `x-panthera-lease`。armd 装一个**服务端拦截器**：对「写/动机械臂类」RPC 校验 header 中的 token 是否等于当前持有者的有效租约，不符直接 `PERMISSION_DENIED`；对「只读类」RPC 一律放行。→ 无需给每条 request 消息加 `lease_token` 字段，proto 保持干净，且校验点唯一。

### 3.2 RPC 持锁分层（显式清单）

| 类别 | 是否需 lease | RPC |
|---|---|---|
| **只读（任意客户端可旁观）** | 否 | `GetJointState` / `GetGripperState` / `StreamState` / `GetControlStatus` / `GetSoftLimits` / `CheckReached` / `GetForwardKinematics` / `GetJacobian` / `GetManipulability` / `GetInverseKinematics` / `PlanCartesianPath` / `GetDynamicsTerm` / `TeachList` / `Heartbeat` |
| **写 / 动机械臂（需 lease）** | 是 | `JointMove` / `JointJog` / `MoveJ` / `JointMIT` / `GripperMove/Open/Close/MIT` / `MoveL` / `CartesianJog` / `RunJointTrajectory` / `TeachStart/Stop/RecordStart/RecordStop/Play` / `SetZero` / `CancelExecution` |
| **安全特例** | 见下 | `EStop` / `ClearEStop` / `ReleaseControl` / `AcquireControl` |

### 3.3 安全特例的显式策略

- **`EStop`：不需持锁**。任何客户端随时可急停，安全优先。（否则 watchdog 自动 release 后操作员反而急停不了——已规避。）
- **`ClearEStop`：需持锁 + `confirm=true`**。proto 加 `confirm` 字段，服务端据此判定，不再是客户端表演。
- **`AcquireControl --force`（抢锁）**：抢锁成功后，armd 对前持有者在飞的 execution **强制 `CancelExecution` 并执行安全收尾**（§4 减速停止），再把租约转移给新持有者；前持有者后续任何写 RPC 因 token 失效被拒。

---

## 4. 停止与安全反应策略（回填 G5）

「速度置零」只能停住速度模式。停止动作**必须按当前控制模式选择**：

| 触发源 \ 当前模式 | 位置模式（`Joint_Pos_Vel`/`moveJ` 在飞） | 速度/MIT 模式（jog/阻抗） | moveL / teach play / 多点轨迹在飞 |
|---|---|---|---|
| **EStop** | `set_stop()`；接受负载关节因重力回落 | `set_stop()` + 前馈/速度归零 | `set_stop()` + 标记 execution=CANCELLED；停止后不再验收轨迹精度 |
| **Watchdog 超时** | 改发「当前位保位」目标（**不是**速度置零，否则电机继续奔向旧位置目标） | 速度/力矩前馈归零 | 触发 `CancelExecution` 的安全减速收尾 |
| **Mode C 新鲜度窗口超时** | —（位置模式不走 C） | 速度/力矩前馈归零 | — |
| **CancelExecution** | — | — | 沿剩余轨迹减速到停（非硬切、非原地悬停在危险位姿） |
| **固件看门狗 150ms** | armd/USB 指令流整体中断后进入电机阻尼模式 | 同左 | 同左；这是软件栈以下的最后一道兜底 |

> M0-2 实测 `set_stop()` 后末端会因负载/重力回落 **3.81–18.91mm**，且不保证回到原姿态。故正常完成时必须继续位置模式保位；轨迹精度在 `set_stop()` **之前**验收。EStop 属紧急卸载，回落量只记录不作为失败。Cancel/watchdog 的平滑收尾算法仍在 M4/M7 前定案（§8 开放项）。

### 4b. 由示例脚本组合而来、非单一 SDK 方法的能力（回填 G13）

以下能力**不对应任何单一 SDK 方法**，是用公开原语拼装、需 armd 自建执行循环的「脚本级组合」，与 `get_Gravity` 这种一对一封装**性质不同**，单列说明其无损口径：

| 能力 | 组合来源 | 自建循环内容 |
|---|---|---|
| `teach start/stop`（自由拖动） | 示例 `2_gravity_friction`：重力+摩擦补偿 + MIT 循环 | HardwareLoop 每周期算 `get_Gravity + get_friction_compensation`，经 `pos_vel_tqe_kp_kd` 前馈，实现「徒手可推动」 |
| `trajectory run-waypoints`（多点轨迹执行） | 示例 `3_interpolation_control`：septic 插值 + 逐点下发 | `septic_interpolation[_with_velocity]` 生成轨迹，HardwareLoop 逐点 `Joint_Pos_Vel(iswait=False)`，每点发 `fraction`/查取消 |
| `cartesian movel` / `teach play` 的**执行** | 见 §1.2 | 复现 `_execute_trajectory` / `_prepare_playback_frames` 逻辑 |
| `estop trigger` / `calibrate zero` | 继承层 `set_stop()` / `set_reset_zero()`（签名待核实，§8） | 直通/单次调用 |

---

## 5. SDK 能力 → CLI / gRPC 总映射表（修订，逐方法核对）

> 覆盖结论先行：42 项 SDK 方法**无一被静默丢弃**。CLI+rpc 映射 33 项直接能力（v1 20 / v2 13），显式不暴露 8 项（均附理由），生命周期不暴露 1 项。**「命令数=方法数」不再作为覆盖论据**（那是巧合，见 §5.12）。

### 5.1 状态查询（8 项，v1）

| # | SDK 方法 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|
| 1 | `get_current_state` | `state get` | `GetJointState` | D | #1–4 合并进一个 `JointState`（全电机原始字段），避免 4 次往返 |
| 2 | `get_current_pos` | `state get` | `GetJointState`（复用） | D | `joints[].position` |
| 3 | `get_current_vel` | `state get` | `GetJointState`（复用） | D | `joints[].velocity` |
| 4 | `get_current_torque` | `state get` | `GetJointState`（复用） | D | `joints[].torque` |
| 5 | `get_current_state_gripper` | `state get --gripper` | `GetGripperState` | D | 独立 rpc（夹爪非同批电机） |
| 6–8 | `get_current_{pos,vel,torque}_gripper` | `state get --gripper` | `GetGripperState`（复用） | D | |

补充：`state watch` 走 `StreamState`（服务端流），是对 #1–8 的持续订阅封装；因 §1.1 循环永不长阻塞，推送始终新鲜（`RobotState` 带 `age_ms`，见 §7 proto）。

### 5.2 关节控制（4 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 9 | `Joint_Pos_Vel` | v1 | `joint move` | `JointMove` | A（`--wait`）/立即返回 | 越限位返回结构化拒绝（关节名+方向+限位值），非裸 bool。`--wait` 走 §1.2 非阻塞步进 |
| 10 | `Joint_Vel` | v1 | `joint jog` | `JointJog` | C | 全关节共用速度数组；CLI 把「点动单关节」翻成整向量。停止靠新鲜度窗口 |
| 11 | `moveJ` | v1 | `joint movej` | `MoveJ` | A/立即返回 | 内部即 `Joint_Pos_Vel`；`--wait` 同 §1.2 |
| 12 | `pos_vel_tqe_kp_kd` | v2 | `joint mit` | `JointMIT` | C | MIT/阻抗原语，供教学模式复用 |

### 5.3 夹爪控制（4 项，回填 G12）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 13 | `gripper_control` | v1 | `gripper move` | `GripperMove` | 立即返回 | SDK 无 `iswait`/`timeout`，不臆造。**「等到位」只轮询 `state get --gripper`**（删除初稿「走 check-reached」——`check_position_reached` 只比关节、不含夹爪，对夹爪不成立）。越限位返回 `reject_reason`（限位值+方向） |
| 14 | `gripper_control_MIT` | v2 | `gripper mit` | `GripperMIT` | 立即返回/可循环 | 专家阻抗 |
| 15 | `gripper_open` | v1 | `gripper open` | `GripperOpen` | 立即返回 | **SDK 缺陷**：`gripper_open` 未 `return` 恒 `None`。armd 直调 `gripper_control(pos=1.6,…)` 绕开，透传真实 bool + `reject_reason` |
| 16 | `gripper_close` | v1 | `gripper close` | `GripperClose` | 立即返回 | 同上绕开缺陷 |

### 5.4 安全 / 校验（2 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 17 | `check_position_reached` | v1 | `safety check-reached` | `CheckReached` | D | 主动刷新一次电机状态再比误差（走 HardwareLoop marshal，见 §1.4）。只比关节，不覆盖夹爪 |
| 18 | `wait_for_position` | 不暴露 | — | — | — | 纯内部辅助，效果已被模式 A 覆盖 |

### 5.5 运动学（6 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 19 | `forward_kinematics` | v1 | `kinematics fk` | `GetForwardKinematics` | D | 返回位置+3×3 旋转+4×4 齐次 |
| 20 | `get_jacobian` | v1 | `kinematics jacobian` | `GetJacobian` | D | 只读诊断（moveL 前查奇异） |
| 21 | `get_manipulability` | v1 | `kinematics manipulability` | `GetManipulability` | D | |
| 22 | `compute_damped_pseudoinverse`(static) | 不暴露 | — | — | — | 纯数学，仅 `CartesianJog` 内部用 |
| 23 | `inverse_kinematics` | v1 | `kinematics ik` | `GetInverseKinematics` | D | 计算 worker + 超时预算（§1.4）；不可达/未收敛返回 `found=false`，try/except 兜底不穿透 gRPC |
| 24 | `rotation_matrix_from_euler`(static) | 不暴露 | — | — | — | 作为 `CartesianPose` 欧拉角字段的服务端编解码约定复用 |

### 5.6 笛卡尔运动（4 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 25 | `compute_cartesian_path` | v1 | `cartesian plan-preview` | `PlanCartesianPath` | D | dry-run，返回 `fraction`+关节轨迹草案。**`avoid_collisions` 已从 CLI 移除**（SDK 中为空操作、恒不做碰撞检测，暴露会误导操作员）；proto 字段保留但标注 `reserved/未实现`，armd 忽略（G9） |
| 26 | `compute_time_parameterization` | 不暴露 | — | — | — | moveL/preview 内部步骤 |
| 27 | `smooth_trajectory_spline` | 不暴露 | — | — | — | moveL 内部平滑 |
| 28 | `moveL` | v1 | `cartesian movel` | `MoveL` | B | **armd 自建 POS-VEL 执行循环**（§1.2），不调 `moveL()`；末点保位收敛后再标 DONE；`StreamExecution` 观察 `fraction`，`CancelExecution` 减速收尾 |

### 5.7 动力学（7 项，v2）

| # | SDK 方法 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|
| 29 | `get_Gravity` | `dynamics gravity` | `GetDynamicsTerm(GRAVITY)` | D | |
| 30 | `get_Coriolis` | `dynamics coriolis` | `GetDynamicsTerm(CORIOLIS)` | D | 矩阵+向量同响应返回 |
| 31 | `get_Coriolis_vector` | `dynamics coriolis` | 同上（`coriolis_vector` 字段） | D | SDK 标注为向后兼容接口，不单开 |
| 32 | `get_Mass_Matrix` | `dynamics mass-matrix` | `GetDynamicsTerm(MASS_MATRIX)` | D | |
| 33 | `get_Inertia_Terms` | `dynamics inertia` | `GetDynamicsTerm(INERTIA)` | D | |
| 34 | `get_Dynamics` | `dynamics inverse` | `GetDynamicsTerm(FULL_INVERSE_DYNAMICS)` | D | |
| 35 | `get_friction_compensation` | `dynamics friction` | `GetDynamicsTerm(FRICTION)` | D | **SDK 缺陷**：`Fc/Fv` 无默认、缺参抛异常。armd 从配置读默认库伦/粘性系数兜底 |

### 5.8 轨迹插值（2 项，v2）

| # | SDK 方法 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|
| 36 | `septic_interpolation`(static) | `trajectory run-waypoints` | `RunJointTrajectory` | B | waypoint 无 `velocity` 时用零边界速度插值 |
| 37 | `septic_interpolation_with_velocity`(static) | 同上 | 同上 | B | waypoint 带 `velocity` 时走此分支；由 `WaypointSpec` 是否填 `velocity` 决定 |

### 5.9 示教录制回放（4 项，v2）

| # | SDK 方法 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|
| 38 | `Recorder.__init__` | `teach record start` | `TeachRecordStart` | 立即返回 | rpc 内部实例化 |
| 39 | `Recorder.log` | （自动） | — | E | HardwareLoop 每周期调，开关走 record start/stop |
| 40 | `Recorder.close` | `teach record stop` | `TeachRecordStop` | 立即返回 | 返回保存路径+帧数 |
| 41 | `Recorder.play`(static) | `teach play` | `TeachPlay` | B | **armd 自建回放循环**（§1.2/§4b），不调 `play()`；`CancelExecution` 定义减速收尾 |

### 5.10 生命周期（不暴露）

| # | SDK 方法 | 理由 |
|---|---|---|
| 42 | `Panthera.__init__` | armd 启动调一次，`config_path` 属部署配置；切配置走部署/重启，不进契约 |

### 5.11 补充映射：架构必需但不在给定清单内的继承层方法

`set_stop()` / `set_reset_zero()` 来自 `htr.Robot` 父类，不在 42 方法清单内、签名待核实（§8）。仍需 v1 落地：

| # | 方法 | 阶段 | CLI | rpc | 备注 |
|---|---|---|---|---|---|
| S1 | `set_stop()` | v1 | `estop trigger` | `EStop` | 最高优先级，经 estop 标志在下一控制周期抢占（§1.3）；不需持锁（§3.3） |
| S2 | `set_reset_zero()` | v1 | `calibrate zero` | `SetZero` | **语义待核实**：是「运动到零位」（危险，需 confirm+可能是模式 B）还是「把当前位重定义为零参考」（不动但平移限位，仍需 confirm）。§8 列为必须核实项，决定 A/B 与 confirm 口径 |

电机层方法（`motor_send_cmd`/`send_get_motor_state_cmd`/`Motors[i].*`）已被 `Panthera` 高阶方法封装，**不单独暴露**（直暴会绕过限位检查与状态缓存）。未来确有电机级诊断需求再单评 `panthera motor debug`。

### 5.12 覆盖口径澄清（回填 G13）

- **不再声称「42 命令 = 42 方法」**：`control*`/`daemon*`/`estop*`/`state watch`/`heartbeat`/`safety limits` 等命令不对应任何 SDK 方法；多个 SDK 方法（合并进 `state get` 的、不暴露的）对应零命令。数字相等纯属巧合，不作覆盖论据。
- **真正的无损主张**：见 §5.1–§5.11 逐行——42 方法每项落到 (a)/(b)/(c) 之一，0 遗漏、0 无理由。
- **脚本级组合能力**（`teach start/stop`、`trajectory run-waypoints`、moveL/play 执行、EStop/SetZero）单列于 §4b，与一对一封装区分。

---

## 6. gRPC deadline 与流的处理（回填 G14a）

- **有界 unary（Mode A/D）**：CLI 按 `timeout_s + 2s` **逐调用**设置 deadline（`joint move --timeout 15` → deadline 17s）。IK 等纯计算 deadline 放宽到 5–10s。**不使用全局默认 deadline**，否则会误杀长动作。
- **长命流**（`StreamState` / `Heartbeat` / `StreamExecution` / `JointJog` / `CartesianJog` / `JointMIT`）：**不设短 deadline**（设无限或极大），生命周期由业务信号（取消/关流/新鲜度窗口）而非 deadline 控制。
- **StreamState 新鲜度**（回填 G14b）：因 §1.1 循环永不长阻塞，缓存每周期刷新；`RobotState` 仍带 `age_ms` 字段，客户端可据此判陈旧并降级显示（如变灰/告警）。

---

## 7. arm.proto 修订草案（关键差异，回填 G6/G9/G10/G12/G14）

> 完整 proto 沿用初稿结构，下列为**审计驱动的修订点**。标量默认值改用 **proto3 `optional`（presence 语义）**，区分「未设→用 SDK/配置默认」与「显式 0」。默认值集中列于 §7.1。lease 走 metadata，故 mutating 消息**不含** `lease_token` 字段。

```proto
// —— IK：optional 表达 presence，消除 proto3 bool 默认 false 反转 SDK 默认 true 的坑（G6）——
message InverseKinematicsRequest {
  CartesianPose target = 1;
  repeated double init_q = 2;
  optional int32  max_iter = 3;          // 未设 → 1000
  optional double eps = 4;               // 未设 → 1e-3
  optional double damping = 5;           // 未设 → 1e-2
  optional bool   adaptive_damping = 6;  // 未设 → true（SDK 默认）
  optional bool   multi_init = 7;        // 未设 → true（SDK 默认）
  optional int32  num_attempts = 8;      // 未设 → 8
}
message InverseKinematicsResponse { bool found = 1; repeated double joint_angles = 2; double error = 3; bool timeout = 4; }

// —— tolerance/timeout 必须 optional：0 有合法含义（严格零容差）——
message CheckReachedRequest { repeated double target_positions = 1; optional double tolerance = 2; } // 未设 → 0.1
message JointMoveRequest {
  repeated double positions = 1; repeated double velocities = 2; repeated double max_torque = 3;
  bool wait = 4; optional double tolerance = 5; optional double timeout_s = 6; // 未设 → 0.1 / 15.0
}
message JointMoveResponse { bool accepted = 1; bool reached = 2; repeated double errors = 3; string reject_reason = 4; }
// MoveJRequest 同样 optional 化 tolerance/timeout_s

// —— 夹爪：补 reject_reason（G12）——
message GripperMoveResponse { bool accepted = 1; string reject_reason = 2; } // 限位值+方向
// GripperOpen/Close 均 returns (GripperMoveResponse)

message MoveLRequest {
  CartesianPose target = 1; optional double duration_s = 2;  // 未设 → 内部估算
  optional bool use_spline = 3;                              // 未设 → true
  repeated double max_torque = 4;
}

// —— plan-preview：avoid_collisions 保留但标注未实现（G9）——
message PlanCartesianPathRequest {
  repeated CartesianPose waypoints = 1;
  bool avoid_collisions = 2 [deprecated = true]; // 未实现：SDK 中为空操作，armd 恒忽略，永不做碰撞检测
}

// —— ClearEStop 加 confirm（G10）——
message ClearEStopRequest { bool confirm = 1; }
// rpc ClearEStop(ClearEStopRequest) returns (EStopResponse);  // 需持锁 + confirm=true

// —— 状态新鲜度（G14b）——
message RobotState { JointState joint = 1; GripperState gripper = 2; int64 age_ms = 3; } // age_ms=缓存新鲜度

// —— teach 文件传输（G14d，v2）——
// 约定：WPF/CLI 只能选 armd/WSL 端已存在文件（TeachList 列举）。跨端上传/下载留待 v2：
message TeachFileChunk { string path = 1; bytes data = 2; bool eof = 3; }
// rpc UploadTeachFile(stream TeachFileChunk) returns (TeachFileInfo);   // v2 可选
// rpc DownloadTeachFile(TeachFileInfo) returns (stream TeachFileChunk); // v2 可选
```

- **DynamicsQueryRequest**：`vel_threshold` optional（未设→0.01）；`fc/fv` 空→配置默认。
- **TeachPlayRequest / TeachRecordStartRequest**：`playback_dt`(0.01)/`smooth_window`(7)/`flush_interval`(0.2)/`gripper_kp`/`gripper_kd`/`mode` 全部 optional 化。
- **StreamStateRequest.rate_hz**：optional（未设→10）。

### 7.1 标量默认值映射表（回填 G6，实现须逐字段对齐）

| 消息.字段 | 未设时默认 | 来源 |
|---|---|---|
| `InverseKinematics.max_iter/eps/damping` | 1000 / 1e-3 / 1e-2 | SDK |
| `InverseKinematics.adaptive_damping/multi_init` | true / true | SDK |
| `InverseKinematics.num_attempts` | 8 | SDK |
| `CheckReached.tolerance`、`JointMove/MoveJ.tolerance` | 0.1 | SDK |
| `JointMove/MoveJ.timeout_s` | 15.0 | SDK（等待上限） |
| `MoveL.use_spline` / `duration_s` | true / 内部估算 | SDK |
| `DynamicsQuery.vel_threshold` | 0.01 | SDK |
| `DynamicsQuery.fc/fv` | 配置文件默认库伦/粘性系数 | armd 配置 |
| `TeachPlay.playback_dt/smooth_window` | 0.01 / 7 | SDK |
| `TeachRecordStart.flush_interval` | 0.2 | 约定 |
| `StreamState.rate_hz` | 10 | 约定 |

---

## 8. panthera-cli 完整命令树（修订）

### v1（24 条）

```
panthera control acquire [--client-id TEXT] [--force]     # --force 抢锁会强制取消前持有者在飞 execution
panthera control release
panthera control status [--json]

panthera estop trigger [--reason TEXT]                    # 不需持锁，任何人可急停
panthera estop reset --confirm                            # 需持锁 + confirm（映射 ClearEStop.confirm）

panthera safety check-reached --target FLOAT_LIST [--tolerance FLOAT]   # 省略 tolerance → 默认 0.1；仅关节，不含夹爪
panthera safety limits show [--json]

panthera calibrate zero --confirm                         # 语义待核实(§10)：可能运动到零位，强制 confirm

panthera state get [--joints/--no-joints] [--gripper/--no-gripper] [--json]
panthera state watch [--rate-hz INT=10] [--joints/--no-joints] [--gripper/--no-gripper]

panthera joint jog --vel FLOAT_LIST [--duration FLOAT] [--interactive]
    # 停止唯一权威 = 250ms 新鲜度窗口；--interactive 的松键即停仅尽力优化(见 §11)
panthera joint move --pos FLOAT_LIST --vel FLOAT_LIST [--max-torque FLOAT_LIST] [--wait] [--tolerance FLOAT] [--timeout FLOAT]
panthera joint movej --pos FLOAT_LIST --duration FLOAT [--max-torque FLOAT_LIST] [--wait] [--timeout FLOAT]

panthera gripper open  [--pos FLOAT=1.6] [--vel FLOAT=0.5] [--max-torque FLOAT=0.5]
panthera gripper close [--pos FLOAT=0.0] [--vel FLOAT=0.5] [--max-torque FLOAT=0.5]
panthera gripper move  --pos FLOAT --vel FLOAT [--max-torque FLOAT=0.5]   # 越限返回结构化 reject_reason

panthera kinematics fk  [--joint-angles FLOAT_LIST]
panthera kinematics ik  --pos FLOAT_LIST [--rpy FLOAT_LIST] [--init-q FLOAT_LIST] [--single-init/--multi-init] [--num-attempts INT=8]
panthera kinematics jacobian [--joint-angles FLOAT_LIST]
panthera kinematics manipulability [--joint-angles FLOAT_LIST]

panthera cartesian movel --pos FLOAT_LIST [--rpy FLOAT_LIST] [--duration FLOAT] [--no-spline] [--max-torque FLOAT_LIST]
panthera cartesian plan-preview --waypoints TEXT          # 已移除 --avoid-collisions（SDK 未实现，避免误导）

panthera daemon status
panthera daemon version
```

### v2（18 条）

```
panthera joint mit   --pos FLOAT_LIST --vel FLOAT_LIST --tqe FLOAT_LIST --kp FLOAT_LIST --kd FLOAT_LIST [--stream FILE]
panthera gripper mit --pos FLOAT --vel FLOAT --tqe FLOAT --kp FLOAT --kd FLOAT
panthera cartesian jog [--linear-vel FLOAT_LIST] [--angular-vel FLOAT_LIST] [--damping FLOAT=0.01] [--interactive]
    # 新鲜度窗口建议更小(如 120ms)：高速笛卡尔 jog 松手漂移更敏感

panthera dynamics gravity     [--joint-angles FLOAT_LIST]
panthera dynamics coriolis    [--joint-angles FLOAT_LIST] [--joint-vel FLOAT_LIST]
panthera dynamics mass-matrix [--joint-angles FLOAT_LIST]
panthera dynamics inertia     [--joint-angles FLOAT_LIST] [--accel FLOAT_LIST]
panthera dynamics inverse     [--joint-angles FLOAT_LIST] [--joint-vel FLOAT_LIST] [--accel FLOAT_LIST]
panthera dynamics friction    --vel FLOAT_LIST [--fc FLOAT_LIST] [--fv FLOAT_LIST] [--vel-threshold FLOAT=0.01]

panthera trajectory run-waypoints --waypoints-file PATH [--durations FLOAT_LIST]

panthera teach start [--kp FLOAT_LIST] [--kd FLOAT_LIST]
panthera teach stop
panthera teach record start [--path PATH] [--flush-interval FLOAT=0.2]
panthera teach record stop
panthera teach play PATH [--kp FLOAT_LIST] [--kd FLOAT_LIST] [--mode mit|posvel] [--playback-dt FLOAT=0.01] [--smooth-window INT=7]
panthera teach list [--json]                              # PATH 为 armd/WSL 端路径；跨端传输见 §7 v2 可选 RPC

panthera camera  stream [--encode h264]                   # 占位，详见未来 camera.proto
panthera dataset export-lerobot --traj PATH --out DIR     # 占位，详见未来 dataset.proto
```

---

# Part 2 — WPF 控制终端设计计划

> **选型已定稿（2026-07-18）：采用稿 C「Fluent 驾驶舱」**（`docs/mockups/mockup-C-fluent-cockpit.html`）作为 WPF v1 的视觉与交互基准。稿 A/B（`docs/mockups/mockup-A-fluent-console.html`、`docs/mockups/mockup-B-fluent-cards.html`）保留作布局与控件参考。三者共享同一套 Fluent 设计语言与数据契约。

## 9. 通用基座（三稿共用）

- **技术栈**：.NET 9 + WPF，Fluent Design；主色 accent `#0067c0`（Windows 蓝）。**三态主题**：系统 / 浅色 / 深色（跟随 §README 要求，`data-theme` 等价的 WPF 资源字典切换）。
- **gRPC 客户端**：与 CLI 同一份 `arm.proto` 生成的 C# stub。
- **权限模型对齐 Part 1 §3**：
  - 启动即以**只读**方式订阅 `StreamState`（无需 lease），任何时候可开监控——即使他人持有控制权，WPF 也能旁观数据采集。
  - 点「获取控制权」才 `AcquireControl`，之后所有写操作经 metadata 带 lease；心跳后台维持，断线/关窗自动 release。
  - **EStop 常驻可用**（红色急停条），不需持锁，随时可点。
- **停止语义对齐 §4/§11**：jog 类控件采用「按住持续发 Mode C 指令，松开/失焦即停」。WPF 能可靠捕获鼠标 up / 键 up / 窗口失焦，故松键即停在 WPF 上是**可靠**的（区别于纯 TTY CLI）；但仍以新鲜度窗口为最终兜底。
- **teach 文件（v2）**：`teach play` 的文件来自 `TeachList` 列举的服务端文件；WPF 提供服务端文件选择器。跨端上传/下载走 §7 的可选 RPC（v2 视需要开）。
- **长动作反馈**：`movel`/`teach play`/多点轨迹提交后，用 `StreamExecution` 的 `fraction` 驱动进度条，`CancelExecution` 挂在「中止」按钮。
- **环境引导（WPF 独有职责，见 WPF_PLAN §3.7）**：WPF 负责「把 USB 挂进 WSL + 拉起 WSL + 启动 armd」的冷启动初始化。**这不是便利功能而是架构必需**——`usbipd`/`wsl.exe` 是 Windows 侧 exe，而项目约定不在 WSL 内跑 exe（interop 挂死），故 armd/CLI 无法自举硬件通道，只有原生跑在 Windows 上的 WPF 能做。流程为「检测→执行→复检」六步幂等引导，按 `VID_CAF1:FFFF`+序列号匹配而非硬编码 busid，特权步骤集中一次提权，且**只建立通道、不下发任何运动指令**。

## 10. 三份视觉稿变体说明

### 稿 A — Fluent 工程控制台（`mockup-A-fluent-console.html`）
- **隐喻**：专业工程上位机。顶栏（连接/控制权/急停）+ 三列工作区 + 底部日志条。
- **布局**：列 1 关节监控（J1–J6 数值/进度/限位），列 2 Jog + 夹爪，列 3 笛卡尔（FK 位姿、moveL 目标、plan-preview）。
- **视觉**：不透明实色面板（`#f3f3f3`/`#ffffff`），高信息密度，弱装饰。
- **适合**：调试期、密集数值核对、单屏尽收全部状态。**信息密度最高、最"工具感"**。

### 稿 B — Fluent 卡片式（`mockup-B-fluent-cards.html`）
- **隐喻**：现代化 Fluent 应用。标题栏含三态主题切换 + 左侧导航 + 主内容列（顶部命令条 + 可滚动卡片）+ 底部日志条。
- **布局**：导航切换「监控 / Jog+笛卡尔 / 示教 …」页；每功能一张半透明 Mica 卡片（`rgba` 卡面 + 柔和投影 `--sh-card`）。
- **视觉**：Mica 半透明质感、卡片分组、留白充裕、`ok/warn/danger` 语义色齐备。
- **适合**：功能会持续扩张（v2 示教/相机/数据集）、需要清晰导航分区、偏产品化交付。**最易扩展、最"成品感"**。

### 稿 C — Fluent 驾驶舱（`mockup-C-fluent-cockpit.html`）✅ 定稿
- **隐喻**：机械臂驾驶舱。顶栏 + 左右仪表列（J1–J3 / J4–J6 圆形仪表）+ 中央雷达俯视台（软限位扇区、距离环、方位刻度、TCP 坐标浮标、JOG 视觉中心、夹爪）+ 底部日志条。
- **布局**：以「末端在工作空间中的位置」为视觉中心，关节量表环绕两侧。
- **视觉**：雷达/扫描/发光装饰，空间直觉最强；数值密度低于 A。
- **适合**：演示/操作直觉优先、需要"末端在哪、离限位多远"的空间感知场景。**空间直觉最强、最"炫"，但实现成本最高（自定义绘制）**。

## 11. jog 交互与键盘后端（回填 G7，跨 CLI/WPF）

- **契约层面**：Mode C 的新鲜度窗口是**唯一权威停止机制**；松键即停/断连/崩溃**一律**靠它兜底。窗口时长可配（关节 250ms、笛卡尔建议 120ms），**松手→停的最坏延迟 = 窗口时长**。
- **CLI `--interactive`**：普通 TTY（cbreak/raw）只有按下自动重复、**收不到 key-up**，SSH/WSL 下无法可靠检测松开。若要真键抬起需 `pynput` 全局钩子（依赖 X/Wayland/Windows 焦点，WSL 纯终端下不可用）。故 CLI 的松键即停**仅尽力优化**，绝不作为安全保证。
- **WPF**：GUI 可可靠捕获鼠标/键 up 与失焦，松键即停体验好；但同样以新鲜度窗口兜底。

---

# 里程碑时间线

> 顺序：**v1 CLI+armd → WPF v1 → v2**。v1 先把「可抢占安全层 + 无损控制」这条最难的骨架跑通，WPF 复用其契约，v2 再叠加示教/相机/数据集。

## 阶段 0 — 架构验证 spike（M0，新增，必须先做）

在写任何业务 RPC 前，用最小原型验证三条决定成败的架构假设：

- **M0-1 非阻塞循环 + 可抢占 EStop** ✅：200Hz 逐周期步进 J1，实测 `set_stop` 抢占延迟 7.73ms。
- **M0-2 自建 moveL 执行循环** ✅：MIT 原方案真机跟踪失败，改为 `compute_cartesian_path` + 逐点 `Joint_Pos_Vel(iswait=False)` + 末点保位；1cm 直线停止前误差 1.73mm，`fraction` 单调，50.1% 取消成功。
- **M0-3 计算/ I/O 分流**：验证 pinocchio 模型能否安全双实例化；跑一次 `multi_init` IK 时确认控制周期不被拖垮（否则切「marshal+超时预算」退化方案）。

**M0 通过是 v1 开工的前置条件。**

## v1（CLI + armd）

- **M1 安全骨架**：`control acquire/release/status`、metadata lease 拦截器、`estop trigger/reset`、`safety limits show`、Heartbeat/watchdog。
  验收：① 两客户端并发 acquire，第二个被拒且能看到持有者 client_id；② **非持锁客户端调 `JointMove` 被 `PERMISSION_DENIED` 拒绝**（lease 校验有牙齿）；③ watchdog 超时后 armd 自动 release，并**按当前控制模式**正确停止（位置模式保位、速度模式归零，§4）；④ `estop trigger` 后下一控制周期内下发的 `JointMove/MoveJ/MoveL` 一律 `REJECTED`，实测抢占延迟 < 100ms，`estop reset --confirm`（持锁）后恢复。
- **M2 状态与标定**：`state get/watch`、`calibrate zero`。
  验收：`state watch` 连续输出、断线重连不 crash、`age_ms` 反映新鲜度；`calibrate zero --confirm` 语义与 §10 核实结论一致。
- **M3 关节/夹爪控制**：`joint jog/move/movej`、`gripper open/close/move`。
  验收：越限 `joint move` 被拒且含关节名+方向+限位值；`joint movej --wait` 到达/超时才返回、误差在 `--tolerance` 内（且**实现为非阻塞步进**，等待期间 watchdog/EStop 仍响应）；`joint jog` 关流或 250ms 无新指令自动停、不漂移；`gripper open/close` 正确反映限位拒绝（非 SDK 恒 `None`）且带 `reject_reason`。
- **M4 笛卡尔与运动学**：`kinematics fk/ik/jacobian/manipulability`、`cartesian movel/plan-preview`、`safety check-reached`。
  验收：`plan-preview` 对不可达路径返回 `fraction<1` 且不执行运动、**无 avoid-collisions 误导**；`movel`（自建执行循环）期间 `StreamExecution` 见 `fraction` 单调递增、`DONE/FAILED/CANCELLED` 三态清晰、取消能减速收尾；`ik` 不可达返回 `found=false` 不穿透异常、超时返回 `timeout=true`。
  **v1 完成口径**：24 条命令可用 + M1–M4 场景全过。

## WPF v1

- **M-W0.5 环境引导**：WSL/usbipd 冷启动引导面板（WPF_PLAN §3.7）。**排在 M-W1 之前**——USB 未挂进 WSL、armd 未起时，监控与控制都无从连起。验收：冷启动状态下一键引导即可到达 armd 健康可连；换 USB 口后仍正确匹配；取消 UAC 能优雅回退。
- **M-W1 只读监控**：按定稿 C（驾驶舱）落地关节圆形仪表 + 中央雷达俯视图 + `state watch` 实时刷新（无需 lease）。自定义绘制（Path/ArcSegment）以 `docs/mockups/mockup-C-fluent-cockpit.html` 为像素级视觉基准。
- **M-W2 控制闭环**：获取控制权 + jog pod 阵列 + `joint move/movej` + 夹爪 + 常驻 EStop + moveL 进度/取消（雷达图 TCP 浮标随动）。
- **M-W3 主题打磨**：系统/浅色/深色三态主题（两套仪表/雷达 token 分别精修，对齐 C 稿）；`prefers-reduced-motion` 等价的动画开关。
  > 注意：C 稿自定义绘制成本为三稿最高——M-W1 先只做静态仪表+数据绑定，扫描线/发光等装饰放 M-W3，不阻塞控制闭环。

## v2

- **M5 阻抗/动力学**：`joint mit`/`gripper mit`、`dynamics *`。验收：各项与直调 SDK 对拍一致；`friction` 缺 `--fc/--fv` 用配置默认不抛异常。
- **M6 多点轨迹**：`trajectory run-waypoints`（自建执行循环）。验收：含/不含中间速度分支正确、可流式观察、可取消。
- **M7 拖动示教录制回放**：`teach start/stop/record*/play/list`。验收：`teach start` 后徒手阻力明显降低；录制 jsonl 字段与 `Recorder.log` 一致；`teach play`（自建回放循环）末端误差可接受、中途 `CancelExecution` 减速收尾。
- **M8 相机流 + LeRobot 导出（占位落地）**：`camera stream`、`dataset export-lerobot`。验收：接口占位打通 + 「录制轨迹→LeRobot 字段」映射草图；正式契约留给独立 `camera.proto`/`dataset.proto`。
- **M9 无损审计收尾**：核对 `.pyi`/`bindings.cpp` 确认 `set_stop`/`set_reset_zero`/电机层真实签名，更新 §5.11；逐行核对方法清单 vs 已实现 rpc，0 遗漏、0 无理由。
- **WPF v2**：示教录制回放面板、D405 视频流、数据采集视图。

---

# 风险与开放问题

## 必须在实现前核实（决定契约取舍）

> **状态更新（2026-07-18）**：第 2/3/4/5 项已通过 SDK 源码逐行核实并结案，结论见文末 **「SDK 源码核实结论」**。第 1 项静态部分已结案（模型为 per-instance，无全局态），运行时部分仍留给 M0-3 实测。核实过程中另发现 6 项计划未覆盖的 SDK 事实（含 1 项安全隐患、1 项 24/7 守护鲁棒性缺陷），一并记入该节。

1. ~~**pinocchio 模型可否安全双实例化**~~ → **已完全结案（M0-3 实测通过，见 §V8）**：双实例安全独立，退化方案不需启用。**并附带一项计划外的架构修正——计算 worker 必须是独立进程而非线程**（GIL 争用会把 500Hz 周期最坏抖到 13.22ms）。IK 超时预算同步由 5s 下调为 0.5s。
2. ~~**`set_reset_zero()` 是否产生运动**~~ → **已结案：不产生运动**。见核实结论 §V2。
3. ~~**继承层签名**~~ → **已结案**，`bindings.cpp` 全量签名见核实结论 §V3。
4. ~~**`MotorState` 字段**~~ → **已结案**，真实结构体字段见核实结论 §V4（proto 须删 `online`、补 `fault`/`mode`/`time`）。
5. ~~**私有方法逻辑复现**~~ → **已结案**：算法见 §V5；M0-2 真机否决了 `_execute_trajectory` 的 MIT 下发方式，规划逻辑保留、执行改用 POS-VEL，详见 §V9。

## 设计待定案

6. **安全收尾算法**（§4）：`CancelExecution`/watchdog 触发时「按剩余轨迹限加加速度减速」vs「回最近安全点」，M4/M7 前定案。
7. ~~**控制周期频率**~~ → **已锁定 200Hz**：M0-1 周期 p50/p95=5.00/5.00ms，max=6.74ms；500Hz 只在纯计算侧成立，真机 I/O 不采用。
8. **teach 文件跨端传输**（§7/§9）：v1/v2 先约定「只选服务端文件」；WPF 是否需要上传/下载 RPC 视使用反馈决定。
9. **WPF 视觉稿选型**：A/B/C 三稿需一次真人试用后定稿（建议 B 为交付主线）。

## 明确排除本次「无损」核对

10. **D405 视频流 / LeRobot 导出**：非 `Panthera`/`Recorder` 方法，是 README v2 路线图；`arm.proto` 仅留最小占位 rpc，正式契约独立成 `camera.proto`/`dataset.proto`，不计入 42 项清单。

---

## 附：审计修订对照（14 项 gap 全部已回填）

| # | 审计缺陷 | 回填位置 |
|---|---|---|
| G1 | Mode A 阻塞钉死 HardwareLoop | §1.1/§1.2/§2（非阻塞步进） |
| G2 | EStop 直通抢占物理不可能 | §1.3（estop 标志 + 下一周期抢占） |
| G3 | moveL/play 无取消缝隙、无进度 | §1.2/§4b/§5.6/§5.9（自建执行循环） |
| G4 | lease_token 未穿进写 RPC | §3.1（metadata 拦截器） |
| G5 | watchdog 速度置零停不住位置模式 | §4（按模式停止策略表） |
| G6 | proto3 默认值反转 IK bool / 歧义 | §7 optional + §7.1 默认值表 |
| G7 | jog 松键即停在 TTY 不可实现 | §2(C)/§11（新鲜度窗口为唯一权威） |
| G8 | 只读/CheckReached 并发线程安全空白 | §1.4（计算/I/O 分流） |
| G9 | plan-preview 暴露未实现的 avoid-collisions | §5.6/§7/§8（CLI 移除、proto 标注未实现） |
| G10 | ClearEStop 缺 confirm/lease、权限未定 | §3.3/§7（EStop 免锁、ClearEStop 需锁+confirm） |
| G11 | 读类 RPC 持锁策略未定义 | §3.2（只读免 lease 分层清单） |
| G12 | 夹爪丢 reject_reason、check-reached 不成立 | §5.3/§7（加 reject_reason、删 check-reached） |
| G13 | 「42 命令=42 方法」巧合框定 | §5.12/§4b（去巧合、单列脚本级组合） |
| G14 | deadline/流/陈旧/set_reset_zero/teach 路径/force-acquire | §6/§7/§3.3/§5.11/§8/§9 |

> 审计结论指出的「HardwareLoop 单线程独占 + 零修改」与「可抢占安全层 + 异步进度/取消」的核心矛盾，由总览三条架构决策 + Part 1 §1–§4 + M0 spike 前置统一收敛。

---

# SDK 源码核实结论（M0 前置，2026-07-18）

> 依据：`Panthera-HT_SDK` @ `main`（`hightorque_robot` whl 1.2.0 / `__cpp_sdk_version__` 4.4.7 / `robot.hpp SDK_version2 = 4.6.0`）。
> 核实对象：`panthera_python/scripts/Panthera_lib/{Panthera,recorder}.py`、`panthera_python/src/bindings.cpp`、`panthera_cpp/motor_cpp/{src/hardware,include}`、`robot_param/*.yaml`、示例脚本。
> 引用格式 `文件:行号`，均为一手核对，非推测。**结论与 README 冲突时以源码为准**（README 已知多处过时，见 §V6-N10）。

## V0. 环境事实（已落地）

- wsl-host 环境已就绪：`uv` venv `~/panthera-wam-env`（Python 3.10.12），已装 `numpy 1.26.4 / pinocchio(pin) 2.7.0 / scipy 1.15.3 / pyyaml 6.0.3` + `hightorque_robot 1.2.0`，`import hightorque_robot` 通过。
- **whl 安装坑**：wheel 内部 METADATA 版本 (1.0.0) 与文件名 (1.2.0) 不一致，`uv` 默认拒装。必须 `UV_SKIP_WHEEL_FILENAME_CHECK=1 uv pip install ...`。
- **依赖包名**：动力学库 PyPI 名是 **`pin`**，import 名是 `pinocchio`（装 `pinocchio` 会装成无关的测试框架）。
- SDK 源码克隆到仓库外（wsl-host `~/Panthera-HT_SDK`），**不进本仓库**。

## V1. 运动学/动力学全部是纯 Python + pinocchio（不碰电机）

`forward_kinematics` / `get_jacobian` / `get_manipulability` / `inverse_kinematics` / `get_Gravity` / `get_Coriolis[_vector]` / `get_Mass_Matrix` / `get_Inertia_Terms` / `get_Dynamics` / `get_friction_compensation` 均只依赖 `self.model`/`self.data`（`Panthera.py:200-239, 604-953, 1384-1513`）。
→ **§1.4「计算/IO 分流」在源码层面完全成立**，Mode D 全部可在计算 worker 上跑，无需 marshal 回 HardwareLoop。
→ 唯一例外：这些方法的 `q=None` 缺省分支会调 `get_current_pos()`（碰电机）。**armd 必须始终显式传 `q`**，否则纯计算调用会偷偷退化成电机 I/O。

**IK 签名已核实，与 §7.1 默认值表完全一致**（`Panthera.py:719-721`）：
`inverse_kinematics(target_position, target_rotation=None, init_q=None, max_iter=1000, eps=1e-3, damping=1e-2, adaptive_damping=True, multi_init=True, num_attempts=8)`，失败/不收敛/迭代越限返回 `None`。**proto 的 IK optional 默认值无需修改**（README 里 `eps=1e-4` 及缺参数是过时文档）。

## V2. `set_reset_zero()` —— 不产生运动（§5.11 S2 结案）

- 协议层：`MODE_RESET_ZERO 0X01 // 重置电机零位`（`serial_struct.hpp:32`）。
- 实现：`robot::set_reset_zero()` 只下发重置零位帧，**无任何位置/速度指令**（`robot.cpp:896-902`）。
- 用法佐证：`0_robot_set_zero.py:37-39` 调用后仅 `motor_send_cmd()` + `sleep(1)` 然后打印状态，**没有等待到位逻辑**。
- **结论**：语义是「把当前物理位置重定义为零参考」，**不运动**。`SetZero` 归入「立即返回」，**不是模式 A/B**。
- **但仍必须 `confirm=true` + 持锁**：它会整体平移零参考，导致 `joint_limits` 全部错位——错误调用后所有软限位预检失效，危险性来自"限位失准"而非"运动"。armd 应在 SetZero 后强制重读状态并告警。
- **两处关键差异**：
  1. `set_reset_zero()` **不自带 flush**（`robot.cpp:896-902` 无 `motor_send_cmd()`），armd 必须紧跟一次 `motor_send_cmd()`。
  2. 全体版**不持久化**；而 `set_reset_zero_motors(ids)` 版会额外 `set_reset()` + `set_conf_write()` 写入电机 flash（`robot.cpp:905-935`）——**全体归零掉电即失效，逐电机归零才持久**。这是 `calibrate zero` 必须向用户讲清的语义。
  3. pybind 文档把参数称为电机 ID，但 C++ 实现实际用 `Motors[motor]` 下标访问（`robot.cpp:905-910`）。armd 对外保持 1..7 电机 ID，调用 SDK 时必须转换为 0..6 下标，避免 ID 7 越界。

## V3. 继承层（`htr.Robot`）真实签名（§5.11 结案，全部 `-> void`）

| 方法 | 签名 | 是否自带 `motor_send_cmd()` | 备注 |
|---|---|---|---|
| `set_stop()` | 无参 | **是**（`robot.cpp:882`） | EStop 单次调用即生效，**无需补 flush** |
| `set_brake()` | 无参 | **是**（`robot.cpp:873`） | 未在 bindings 暴露 |
| `set_reset()` | 无参 | 否，但**内部 sleep 200ms**（`robot.cpp:892`） | 重启电机；**阻塞，禁止在控制循环内调用** |
| `set_reset_zero()` | 无参 | **否** | 见 §V2 |
| `set_reset_zero_motors(ids)` | `list[int]` | 否 | 逐电机 + 持久化 |
| `set_timeout(ms)` | `int16` | 否，**内部 5×10ms = ~50ms 阻塞**（`robot.cpp:938-948`） | 只能在 init 调用 |
| `motor_send_cmd()` / `send_get_motor_state_cmd()` / `send_get_motor_version_cmd()` | 无参 | — | |
| `get_motors()` / `get_motor_by_id(id)` / `get_motor_by_name(name)` | — | — | |

→ **`EStop` proto 保持最小占位即可，无需改**。`set_stop()` 自带 flush 这一点直接支撑 §1.3 的「一个控制周期 + 一次电机写」延迟模型。

## V4. `MotorState` 真实字段（§8-4 结案，proto 必须改）

C++ `motor_back_t`（`serial_struct.hpp:212-222`）+ pybind 暴露面（`bindings.cpp:47-61`）：

| 字段 | 类型 | 说明 | proto 处置 |
|---|---|---|---|
| `time` | double | 电机侧时间戳（秒） | **新增**，配合 §6 `age_ms` |
| `ID` | uint8 | 电机 ID（**大写 ID**） | 映射到 `motor_id` |
| `mode` | uint8 | 运行模式（`MODE_*` 常量） | **新增**——直接给出 §4「按当前控制模式选择停止动作」所需的模式判据 |
| `fault` | uint8 | 故障码 | **新增**——WPF §3.3 关节级 fault 显示所需 |
| `position` / `velocity` / `torque` | float | rad / rad·s⁻¹ / N·m | 保留 |
| `num` | int | C++ 有但 **pybind 未暴露** | 忽略 |

- **`online` 字段不存在，proto 必须删除**（原为臆测占位）。
- 在线性改判据：`publishJointStates` 用 `now - state.time < 0.1` 判新鲜，否则填 `-999.0`（`robot.cpp:206-215`）；`position == 999.0f` 是「未连接」哨兵（`robot.cpp:830`）。**armd 必须把 999.0 当无效值处理，不能当真实位置推给客户端。**
- 另有 `Motor.pos_limit_flag`（0 正常 / 1 超上限 / **-1 超下限**）与 `Motor.tor_limit_flag`，pybind 只读暴露（`bindings.cpp:173-176`）——**正好用于结构化限位拒绝原因与 WPF 仪表告警态**。注意 `motor.hpp:131` 注释漏写 `tor_limit_flag = -1` 分支，实际存在（`motor.cpp:581`）。

## V5. 私有方法算法（§8-5 结案，等价重写依据）

**`_execute_trajectory`（`Panthera.py:1321-1379`）—— moveL 的执行内核，用 MIT 而非 Pos_Vel**：
逐点：忙等到 `timestamps[i]` → `tqe = clip(get_Gravity(traj[i]), ±max_tqu)` → `pos_vel_tqe_kp_kd(traj[i], vel[i], tqe, kp, kd)`，其中 **硬编码 `kp=[30,50,60,25,15,10]`、`kd=[3,5,6,2.5,1.5,1]`**，`max_tqu` 缺省回落 `[21,36,36,21,10,10]`。（Pos_Vel 分支在源码中已被注释掉。）

→ **M0-2 真机修正**：以上是 SDK 的真实实现，但不是 armd 应复制的正确执行策略。当前电机固件 v4.7.3 下，自建 MIT 路径 2s 收敛后仍有 9.53mm 目标误差；POS-VEL 路径误差 1.73mm。故 v1 moveL 采用 POS-VEL，MIT 仅保留给 v2 专家阻抗/示教能力。

**`Recorder.play`（`recorder.py:46-115`）**：取帧 → `_prepare_playback_frames` → 夹爪先 `gripper_control(start,0.5,0.5)` + **硬编码 `sleep(2.0)`** → `Joint_Pos_Vel(start, [0.5]*n, max_torque, iswait=True, tolerance=0.05, timeout=30.0)` → 逐帧忙等后按 `mode` 下发（mit：重力+可选摩擦补偿+可选 `tau_limit` 钳位；posvel：`Joint_Pos_Vel`）+ `gripper_control_MIT(..., 0.0, gripper_kp, gripper_kd)`。
默认值：`playback_dt=0.01`、`smooth_window=7`、`gripper_kp=5.0`、`gripper_kd=0.5`、**`vel_threshold=0.0`（注意与 `get_friction_compensation` 的 0.01 不同）**、`mode="mit"`；`Recorder.__init__` 的 `flush_interval=0.2`。

**`_prepare_playback_frames`（`recorder.py:117-165`）**：剔除非递增时间戳（`diff > 1e-6`）→ 按 `playback_dt` 用 `np.interp` 逐关节重采样 → `_moving_average` → `vel = np.gradient(pos, t, axis=0)`；夹爪同理。
**`_moving_average`（`recorder.py:167-176`）**：窗口强制为奇数，`np.pad(mode="edge")` 补 `window//2`，均匀核 `np.convolve(..., "valid")`。

**§4b 脚本级组合的真实配方**：
- `teach start`（自由拖动）= `2_gravity_friction_compensation_control.py`：**`kp`/`kd` 全零**（纯力矩模式）+ `tqe = clip(get_Gravity() + get_friction_compensation(vel,Fc,Fv,vel_threshold), ±[15,30,30,15,5,5])`，循环 `sleep(0.005)`（≈200Hz）。参考系数 `Fc=[.20,.15,.15,.15,.04,.04]`、`Fv=[.06,.06,.06,.03,.02,.02]`、`vel_threshold=0.02` —— **正好用作 §5.7 #35 要求的 armd 配置兜底默认值**。
- `trajectory run-waypoints` = `3_interpolation_control_zeroVel.py`：`control_rate=100Hz`，逐段 `septic_interpolation` → **`Joint_Pos_Vel`（非 MIT，与 moveL 不同）**，用绝对时间基 `segment_start + (step+1)*dt` 防累积漂移。

## V6. 计划未覆盖的新发现（N1–N4 影响架构，须处置）

**N1（安全隐患，当前不触发但必须盯住）**：`robot::motor_send_cmd()` 被限位标志门控——`if(!motor_position_limit_flag && !motor_torque_limit_flag)` 才真正下发（`robot.cpp:250-260`）。而 `set_stop()` 结尾正是 `motor_send_cmd()`，**理论上限位латch 一旦置位，连 EStop 都会静默失效**，且这两个标志 **未在 bindings 暴露、Python 既读不到也清不掉**。
→ **实测结论：唯一置位点 `detect_motor_limit()` 在整个 C++ 源码中「零调用点」（仅有定义与声明），当前是死代码**；叠加 N3（限位使能全 false），标志恒为 0，**EStop 当前安全**。
→ **处置**：① 锁定 SDK 版本并记录本结论；② armd 增加启动自检/回归检查，一旦上游把 `detect_motor_limit()` 接入调用链，必须立即重新评估 EStop 可靠性；③ M0-1 的验收项里显式加一条「EStop 在限位触发后仍然有效」的对照观察。

**N2（安全增益，已采纳）**：电机固件自带硬件看门狗 `motor_timeout_ms`，超时未收到新指令则**进入阻尼模式**；取值 `[0, 32760]` ms，SDK 默认配置为 `0`＝禁用（`6dof_Panthera_params_follower.yaml:5`）。
→ 用户已确认采用 **150ms**：大于 200Hz 控制周期 5ms，且小于关节 jog 250ms 新鲜度窗口。`RealBackend` 仅在初始化期调用一次 `set_timeout(150)`（内部约阻塞 50ms，且其 CANport 实现自行发送，无需额外 `motor_send_cmd()`）；部署值写入 `deploy/armd.env.example`。这是 armd 进程整体死亡后仍生效的软件栈以下兜底。

**N3（安全事实）**：7 个电机的 `pos_limit_enable` / `tor_limit_enable` **全部为 `false`**（`6dof_Panthera_params_follower.yaml`），`pos_upper/lower=±5`、`tor_upper/lower=5/-3` 均为占位。
→ **硬件层当前没有任何限位保护网**，全部限位保护来自 Python 层 `Panthera.joint_limits`。这使 §「软限位入队前预检」从「加固」升级为**唯一防线**，不可省略、不可降级。

**N4（24/7 守护鲁棒性缺陷）**：`robot` 构造时起了后台 `check_error` 线程（1s 轮询，`robot.cpp:429-545`）；串口异常时会 `CANboards.clear() / CANPorts.clear() / Motors.clear()` 并 `delete` 串口对象，随后重建全新 motor 对象（`robot.cpp:457-526`）。
→ 而 `Panthera.__init__` 只在初始化时缓存一次 `self.Motors = self.get_motors()`（`Panthera.py:54`）。**重连后 Python 侧持有的是已析构对象的悬垂引用**——对短脚本无感，对常驻 armd 是致命的 use-after-free。
→ **处置已落地**：`RealBackend` 不跨控制周期缓存 SDK 的 motor 指针；每周期刷新状态和每次写帧前重新 `get_motors()`，固件版本每秒复核。重连窗口内电机数不为 7 时返回 7 个 `999.0` 无效快照并拒绝控制，恢复后自动换用新句柄，避免继续解引用已析构对象。

**N5**：`send_get_motor_state_cmd()` 在旧固件（`fun_v < fun_v2`）分支下会 **给所有电机下发 `velocity(0.0)` 再 flush**（`robot.cpp:599-606`）——「读状态」在旧固件上是**带副作用的写**。`RealBackend` 读取 7 个 `Motor.get_version()`，最低版本低于 4.2.0 时拒绝启动/控制；版本尚未读全时不发状态查询并对外返回无效快照。

**N6（拓扑）**：实际为 **1 CANboard / 1 CANport / 7 电机**（`joint1..joint7`，波特率 4,000,000），`joint7` 即夹爪（`Panthera.gripper_id = len(Motors) = 7`，`motor_count = 6`）。CLAUDE.md 记的「7×虚拟串口」应理解为 USB 复合设备暴露多个 ttyACM，SDK 只用 `serial_id=1`；`check_serial_dev_exist` 要求 `/dev/ttyACM0..7` 中至少 4 个存在。部署需 udev 规则 `KERNEL=="ttyACM*", MODE="0777"`。

**N7（已核实成立，直接改写 HardwareLoop 下发编排）—— 整帧单模式，切模式会抹掉其它电机指令**

证据链：
- `canport` 只持有**一帧** `cdc_tr_message`，`canport::motor_send_cmd()` 就是 `ser->send_2(&cdc_tr_message)` 一次整帧发送（`canport.cpp:422-425`）。
- 同端口所有电机共享该帧指针 `p_cdc_tx_message`，各自只写 `data.<union>[MEM_INDEX_ID(id)]`（`id-1` 槽位）。
- **每个控制方法都带同一段逻辑**（`motor.cpp:396-539`）：若 `head.s.cmd != MODE_X`，则改写帧头为 `MODE_X` 并**把整个数据区清成 `0x8000`**（int16 模式）或 `0`（字节模式），随后只填自己那一格。

结论：
1. **一帧只有一个控制模式**，`0x8000` 是「本电机无指令」哨兵（**不是零位置**）。
2. **跨模式写入会静默抹掉同端口其它电机已写好的槽位**。本机 7 个电机（6 关节 + 夹爪）全部挂在同一个 CANport 上，因此「关节走 MIT + 夹爪走 pos-vel」在同一周期内**不可能共存**：后写的一方会把先写的一方清空，`motor_send_cmd()` 发出去的帧里先写方无指令。
3. SDK 自身示例之所以没暴露这个坑，是因为配对恰好同模式（`Recorder.play` 用 `pos_vel_tqe_kp_kd` + `gripper_control_MIT` 同为 5 参数模式；`Joint_Pos_Vel` + `gripper_control` 同为 `pos_vel_MAXtqe`）——**属于巧合而非机制保证**。

**对 armd 的硬性约束（写入 §1.1 步骤 4）**：
- HardwareLoop 必须维护「本端口当前帧模式」这一单一状态；**每个控制周期用同一模式写满全部 7 个槽位（含夹爪），然后只调一次 `motor_send_cmd()`**。
- 夹爪指令**必须表达成关节当前所处的模式**（pos-vel 时用 `gripper_control`，MIT 时用 `gripper_control_MIT`）——两种表达都存在，故总是可达，不需要拒绝夹爪操作。
- 反过来，**禁止**在一个周期内混用 `Joint_Pos_Vel` 与 `pos_vel_tqe_kp_kd`，也禁止在关节 MIT 执行期间直接调 `gripper_control`。
- `set_stop()` 把帧切到 `MODE_STOP` 并清空数据区，**恰好构成一次干净的整帧抢占**——这从组帧层面进一步支撑 §1.3 的 EStop 延迟模型。
- 该共享帧**无任何锁**。这独立验证了「HardwareLoop 单线程独占 `Panthera` 对象」不是风格选择而是**正确性要求**：任何第二个线程碰电机都会撕裂这一帧。

**N8（示例脚本陷阱）**：多个官方示例在 `KeyboardInterrupt` 分支里把 `robot.set_stop()` **注释掉了**却仍打印「所有电机已停止」（`2_gravity_friction_compensation_control.py:96,100`；`3_interpolation_control_zeroVel.py:95`）。即中断后电机保持最后指令。**不可照抄示例的收尾逻辑**。

**N9（确认原计划判断）**：`gripper_open`/`gripper_close` 调用 `gripper_control` 但**未 `return`，恒为 `None`**（`Panthera.py:563-569`），而 `gripper_control` 越限返回 `False`（`:519-539`）。§5.3「armd 直调 `gripper_control` 绕开」的方案**成立且必要**。同理确认 `compute_cartesian_path` 的 `avoid_collisions` 形参**在函数体内从未被引用**（`:958-997`），G9 处置正确。

**N10（文档/配置陷阱）**：① `Panthera()` 默认配置是 **`Follower.yaml`**（`Panthera.py:36`），README 称 Leader 是错的。② `Follower.yaml` 的 `control:`（`position_tolerance/timeout` 等）**全程未被任何代码读取**，是死配置，不可假定生效。③ README 的 API 章节多处过时（IK 签名、`gripper_open` 参数、`quintic_interpolation` 实际不存在）。

## V7. 真实配置基线（`Follower.yaml` + 电机参数，armd 默认值来源）

| 项 | 值 |
|---|---|
| `joint_limits.lower` | `[-2.4, -0.1, -0.1, -1.6, -1.7, -2.5]` |
| `joint_limits.upper` | `[2.4, 3.2, 4.0, 1.6, 1.7, 2.5]` |
| `gripper_limits` | `[0.0, 2.0]`（`gripper_open` 默认 1.6 在范围内） |
| `max_torque` | `[21, 36, 36, 21, 10, 10]` |
| `velocity_limits` | `[1,1,1,1,1,1]` rad/s（jog 超此值会被 `Joint_Vel` 静默钳位） |
| `acceleration_limits` | `[2,2,2,2,2,2]` |
| `moveit_cartesian` | `eef_step=0.002m`、`jump_threshold=1.5rad`、`resample_dt=0.01s` |
| URDF / EEF | `Panthera-HT_description_follower.urdf`、`base_link` → `tool_link` |
| 关节名 | `joint1..joint6`（`joint7`=夹爪） |

> **注意 J2/J3 下限均为 -0.1**（几乎不能反向），jog/限位 UI 的可视化范围需按此非对称区间设计。

## V8. M0-3 实测结果（2026-07-18，纯计算，未碰硬件）

脚本 `spikes/m0/m0_3_compute_split.py`，wsl-host / Python 3.10.12 / pinocchio 2.7.0。
模型：`Panthera-HT_description_follower.urdf`，`nq/nv = 6/6`，EEF frame id = 16。

**Q1 双 pinocchio 实例独立性 —— 通过 ✅**
`model`/`data` 互不为同一对象；对实例 A 调 `get_Gravity`（内部临时改写 `model.gravity` 再还原）后，A 的 gravity 已还原、**B 完全未受影响**；两实例对同一 `q` 的 FK 结果一致。
→ §1.4「专用第二实例」成立，**退化方案（计算 marshal 回 HardwareLoop）不需要启用**。

**Q2 `inverse_kinematics(multi_init=True, num_attempts=8)` 墙钟耗时**

| 目标 | p50 | p95 | max |
|---|---|---|---|
| 可达（由 FK 取点，保证收敛） | 2.2 ms | 4.7 ms | 7.4 ms |
| 不可达（跑满 attempts×max_iter 最坏路径） | 148.9 ms | — | **170.4 ms** |

→ 原定 5s 超时预算过宽，**下调为 0.5s**（含 3 倍余量）。IK 远没有计划假设的「秒级」那么慢。

**Q3 控制循环抖动（目标 500Hz = 2.0ms/周期，每档 3s，循环内做 FK + `get_Gravity`）**

| 场景 | 周期 p50 | 周期 p95 | 周期 max | worker 完成 IK 次数 |
|---|---|---|---|---|
| 基线（无并发） | 2.00 ms | 2.00 ms | 2.10 ms | — |
| IK 跑在**同进程线程** | 2.00 ms | **3.43 ms** | **13.22 ms** | 643 |
| IK 跑在**独立进程** | 2.00 ms | 2.00 ms | 2.05 ms | **1668** |

→ **结论：计算 worker 必须用独立进程。** 线程方案因 GIL 争用把 p95 抬高 1.72×、最坏抖动达 6.6 个控制周期，且 IK 自身吞吐还低 2.6 倍——两头都输。进程方案与基线无法区分。此结论已回写 §1.4。

> **关于控制周期频率（§8-7）**：本测试只证明纯计算侧 500Hz 无压力；真机 M0-1 已将最终频率锁定为 **200Hz**，见 §V9。

## V9. M0-1 / M0-2 真机实测结果（2026-07-18）

环境：CANboard v4.8.6，7 个电机固件均为 v4.7.3，USB `caf1:ffff`，WSL2 / Python 3.10.12。

### V9.1 M0-1：非阻塞循环 + EStop —— 通过 ✅

- 动作：J1 正向 2°，200Hz，计划 4s，第 1.5s 置 estop 标志。
- `estop flag → set_stop() 返回`：**7.73ms**，满足 <100ms。
- 控制周期：p50=5.00ms，p95=5.00ms，max=6.74ms。
- 结论：真机控制周期锁定 **200Hz**；EStop 经下一周期抢占成立。

### V9.2 M0-2：自建 moveL —— 改方案后通过 ✅

统一测试路径：当前 TCP 沿 Z 轴 +1cm，4s，规划 6 点并样条重采样为 401 点，规划 `fraction=1.0`。

| 执行方式 | 停止前结果 | 结论 |
|---|---|---|
| 自建 MIT（复刻 SDK `_execute_trajectory`） | 2s 收敛后仅移动 0.91mm，目标误差 **9.53mm** | **否决**，当前固件跟踪不可靠 |
| 自建 POS-VEL + 末点保位 | 实际移动 8.31mm，目标误差 **1.73mm**，各关节误差 <0.3° | **采用** |
| POS-VEL 50.1% 取消 | `fraction` 严格单调；停止前移动 3.46mm；终态 CANCELLED | 可取消成立 |

执行与验收规则：

1. moveL 正式执行原语改为 `Joint_Pos_Vel(iswait=False)`；MIT 不用于 v1 轨迹跟踪。
2. 轨迹末点继续位置模式保位，直到进入关节容差或超时；不能在最后一个样条点发完后立即 `set_stop()`。
3. SDK `check_position_reached()` 发查询后立即读缓存，存在陈旧状态假阳性；armd 必须等待/按时间戳确认新状态后再判到达。
4. `set_stop()` 后实测末端因重力回落 **3.81–18.91mm**，且不保证回到原姿态。轨迹精度必须在停止前验收；EStop 后回落只记录，不要求精确复位。
