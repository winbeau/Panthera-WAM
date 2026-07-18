# Panthera-WAM：panthera-cli + armd gRPC 契约计划（v1 / v2 两阶段）

> 状态：规划阶段，本文档不含实现代码。
> 目标：SDK（`Panthera` 类 + `Recorder` 类，共 42 个公开方法/静态方法）的每一项能力，
> 都必须在本文档中落到下列三种归宿之一：
> **(a) 有对应的 panthera-cli 命令 + arm.proto rpc**；
> **(b) 显式标注"不暴露"并给出理由**（私有实现细节 / 双臂功能单臂不适用 / 已被更高阶封装 / 纯工具函数无独立语义）；
> **(c) 显式标注"v2 占位"**（非本仓库当前 SDK 清单内、但架构文档明确要求的能力，如 D405 视频流、LeRobot 导出，留待独立 proto 契约）。
>
> 覆盖状态可在第 2 章的总映射表中逐行核对，第 7 章给出已知信息缺口与后续核实动作。

---

## 0. 范围界定

- 硬件与线程模型、安全层（AcquireControl 互斥 / watchdog / 软限位预检 / EStop 直通）均沿用 README 已定架构，不重新论证。
- 本计划中的 "SDK" 特指任务给出的方法清单：`Panthera.py`（38 个方法，含 4 个 staticmethod）+ `recorder.py`（`Recorder` 类 4 个方法）。
- `Panthera` 继承自 `htr.Robot`（pybind11 编译扩展），示例脚本里直接调用的 `set_stop()` / `set_reset_zero()` / `motor_send_cmd()` 等继承/组合层方法**不在**给定清单内、其真实签名未核实。架构明确要求 EStop 与回零两项能力，因此第 2.3 节把它们作为"补充映射"单列，并在第 7 章标注核实动作，不假装已完整覆盖这一整层。
- D405 视频流、LeRobot 数据导出不是 `Panthera`/`Recorder` 的方法，是 README 里明确的 v2 路线图条目；本计划仅给出 CLI 占位与字段草图，详细契约建议独立成 `camera.proto` / `dataset.proto`，不在 `arm.proto` 的"无损"核对范围内计数。

---

## 1. 阻塞语义 → gRPC 表达：五种模式

SDK 里各方法的阻塞行为差异很大（有的 `iswait` 可选且有 15s 上限，有的天生阻塞到整条轨迹跑完，有的纯计算，有的是"服务端每周期自己调"）。逐方法生搬硬套一种 gRPC 风格会既不安全也不好用，因此定义五种模式，第 2 章每行标注用哪种：

| 模式 | 适用场景 | gRPC 形状 | 说明 |
|---|---|---|---|
| **A｜服务端等待** | 有限上限等待（SDK 自带 `timeout`，默认 ≤15s）：`Joint_Pos_Vel`/`moveJ` 的 `iswait=True` | 普通 unary，服务端内部按 SDK 语义阻塞到 `timeout` | gRPC deadline 建议设为 `timeout + 2s` 缓冲；对客户端最简单，行为与 SDK 1:1 |
| **B｜立即返回 + 执行流 + 可取消** | 无固定上限或耗时较长：`moveL`、`Recorder.play`、多点轨迹 | unary 提交返回 `execution_id` → `StreamExecution(execution_id)` 服务端流推送 `fraction/state` → `CancelExecution` 随时中止 | 避免长 unary 占用连接；取消需要 HardwareLoop 能安全收尾（减速停止而非硬切） |
| **C｜双向流（持续控制意图）** | 需要客户端持续给"意图"的连续控制：`Joint_Vel` jog、`pos_vel_tqe_kp_kd` 阻抗/MIT、笛卡尔速度 jog | `stream Command → stream Feedback` | 服务端设置指令新鲜度窗口（如 250ms），超时未收到新指令则自动把速度/力矩前馈归零，是 watchdog 思想在流级别的落地 |
| **D｜纯计算只读** | 无 I/O 等待的同步计算：FK/IK/Jacobian/可操作度/动力学各项/路径规划预览 | 普通 unary，立即返回 | `inverse_kinematics(multi_init=True)` 可能到秒级，client deadline 建议放宽到 5–10s |
| **E｜服务端内部自驱动，非逐次 RPC** | `Recorder.log()` 这类"每个控制周期都要调用"的方法 | RPC 只做开关（打开/关闭标志位），真正的调用发生在 armd 的 `HardwareLoop` 内部，不经过网络 | 避免为录制/回放把状态逐帧搬上网络再搬回去的无意义开销 |

---

## 2. SDK 能力 → CLI / gRPC 总映射表（核心，逐方法核对）

### 2.1 状态查询（8 项，均 v1）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 1 | `get_current_state` | v1 | `state get` | `GetJointState` | D | 与 #2-4 合并到同一个 `JointState` message（含全部电机原始字段），避免 4 次往返 |
| 2 | `get_current_pos` | v1 | `state get` | `GetJointState`（复用） | D | 字段包含在 `JointState.joints[].position` |
| 3 | `get_current_vel` | v1 | `state get` | `GetJointState`（复用） | D | `JointState.joints[].velocity` |
| 4 | `get_current_torque` | v1 | `state get` | `GetJointState`（复用） | D | `JointState.joints[].torque` |
| 5 | `get_current_state_gripper` | v1 | `state get --gripper` | `GetGripperState` | D | 独立 rpc（夹爪与关节非同一批电机） |
| 6 | `get_current_pos_gripper` | v1 | `state get --gripper` | `GetGripperState`（复用） | D | |
| 7 | `get_current_vel_gripper` | v1 | `state get --gripper` | `GetGripperState`（复用） | D | |
| 8 | `get_current_torque_gripper` | v1 | `state get --gripper` | `GetGripperState`（复用） | D | |

补充：`state watch` 走 `StreamState`（服务端流，可配置频率），非 SDK 单独方法，是对 #1-8 的持续订阅封装。

### 2.2 关节控制（4 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 9 | `Joint_Pos_Vel` | v1 | `joint move` | `JointMove` | A（`wait=true`时）/ 立即返回（`wait=false`，退化为普通 unary） | 越限位时 SDK 返回 `False`，armd 需把拒绝原因（关节名+方向+限位值）结构化返回，不能只给 bool |
| 10 | `Joint_Vel` | v1 | `joint jog` | `JointJog` | C | 全部关节共用一个速度数组，不是单关节 API；CLI 内部把"点动某一关节"翻译成"其余关节速度置 0"的完整向量 |
| 11 | `moveJ` | v1 | `joint movej` | `MoveJ` | A（`wait=true`）/ 立即返回 | 内部即 `Joint_Pos_Vel`，语义相同，独立 CLI 名是为了保留"给定 duration 匀速同步到达"的直觉 |
| 12 | `pos_vel_tqe_kp_kd` | v2 | `joint mit` | `JointMIT` | C | MIT 模式属于专家级/阻抗控制原语，v1 不需要；给教学模式(2.5节)复用 |

### 2.3 夹爪控制（4 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 13 | `gripper_control` | v1 | `gripper move` | `GripperMove` | 立即返回 | SDK 本身无 `iswait`/`timeout` 参数，不臆造；CLI 若要"等到位"，走客户端轮询 `state get --gripper` 或 `safety check-reached`，不新增服务端能力 |
| 14 | `gripper_control_MIT` | v2 | `gripper mit` | `GripperMIT` | 立即返回（可循环调用做连续阻抗） | 与 #12 同级专家能力 |
| 15 | `gripper_open` | v1 | `gripper open` | `GripperOpen` | 立即返回 | **已知 SDK 缺陷**：`gripper_open` 内部调用 `gripper_control` 但未 `return`，恒为 `None`。armd 实现 `GripperOpen` 时直接调用 `gripper_control(pos=1.6,...)`，不经过有缺陷的包装，从而能把成功/限位拒绝的真实 bool 透传给客户端 |
| 16 | `gripper_close` | v1 | `gripper close` | `GripperClose` | 立即返回 | 同上，绕开 `gripper_close` 的同一缺陷 |

### 2.4 安全 / 状态校验（2 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 17 | `check_position_reached` | v1 | `safety check-reached` | `CheckReached` | D | 主动刷新一次电机状态后比较误差 |
| 18 | `wait_for_position` | 不暴露 | — | — | — | 纯内部辅助，只服务于 `iswait=True`；其效果已由模式 A（服务端等待）完整覆盖，无需独立 rpc |

### 2.5 运动学（6 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 19 | `forward_kinematics` | v1 | `kinematics fk` | `GetForwardKinematics` | D | 返回位置+3x3旋转矩阵+4x4齐次变换，供客户端换算显示 |
| 20 | `get_jacobian` | v1 | `kinematics jacobian` | `GetJacobian` | D | 只读诊断，划入 v1 监控/安全范畴（moveL 前检查奇异性） |
| 21 | `get_manipulability` | v1 | `kinematics manipulability` | `GetManipulability` | D | 同上 |
| 22 | `compute_damped_pseudoinverse` (static) | 不暴露独立 rpc | — | — | — | 纯数学工具函数；只作为 v2 `CartesianJog`（2.7节）服务端内部实现的一部分，无独立业务语义 |
| 23 | `inverse_kinematics` | v1 | `kinematics ik` | `GetInverseKinematics` | D（client deadline 建议 5-10s，因 `multi_init=True` 可能迭代较久） | 不可达/未收敛返回 `found=false`，armd 必须 try/except 兜底，不能让 SDK 异常直接打断 gRPC 连接 |
| 24 | `rotation_matrix_from_euler` (static) | 不暴露独立 rpc | — | — | — | 纯姿态转换工具；作为 `CartesianPose` message 里"欧拉角输入"字段的服务端编解码约定复用，不设 endpoint |

### 2.6 笛卡尔运动（4 项）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 25 | `compute_cartesian_path` | v1 | `cartesian plan-preview` | `PlanCartesianPath` | D | dry-run，不执行，返回 `fraction`+关节轨迹草案，供上层在真正 `movel` 前做安全预检 |
| 26 | `compute_time_parameterization` | 不暴露 | — | — | — | `moveL`/`plan-preview` 流水线内部步骤，无独立执行语义；单独暴露会绕开轨迹生成的既定顺序 |
| 27 | `smooth_trajectory_spline` | 不暴露 | — | — | — | 同上，`moveL` 内部平滑步骤 |
| 28 | `moveL` | v1 | `cartesian movel` | `MoveL` | B | 无固定上限，必须异步：提交后 `StreamExecution` 观察 `fraction`，`CancelExecution` 可中止（HardwareLoop 需实现安全减速停止） |

### 2.7 动力学（7 项，均 v2）

服务于"拖动示教"的重力/摩擦补偿自由驱动模式，也可作为独立诊断工具核对阻抗参数。

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 29 | `get_Gravity` | v2 | `dynamics gravity` | `GetDynamicsTerm(term=GRAVITY)` | D | |
| 30 | `get_Coriolis` | v2 | `dynamics coriolis` | `GetDynamicsTerm(term=CORIOLIS)` | D | 响应里矩阵与向量字段一起返回（见 #31） |
| 31 | `get_Coriolis_vector` | v2 | `dynamics coriolis` | 同上（复用响应的 `coriolis_vector` 字段） | D | SDK 注释其为"向后兼容接口"，不必单开 rpc |
| 32 | `get_Mass_Matrix` | v2 | `dynamics mass-matrix` | `GetDynamicsTerm(term=MASS_MATRIX)` | D | |
| 33 | `get_Inertia_Terms` | v2 | `dynamics inertia` | `GetDynamicsTerm(term=INERTIA)` | D | |
| 34 | `get_Dynamics` | v2 | `dynamics inverse` | `GetDynamicsTerm(term=FULL_INVERSE_DYNAMICS)` | D | |
| 35 | `get_friction_compensation` | v2 | `dynamics friction` | `GetDynamicsTerm(term=FRICTION)` | D | **已知 SDK 缺陷**：`Fc`/`Fv` 无默认值，缺参直接抛异常。armd 层必须从配置文件读取默认库伦/粘性系数兜底，不能把裸异常透传给 gRPC 客户端 |

### 2.8 轨迹插值（2 项，v2）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 36 | `septic_interpolation` (static) | v2 | `trajectory run-waypoints` | `RunJointTrajectory` | B | waypoint 不带 `velocity` 时用此零边界速度插值 |
| 37 | `septic_interpolation_with_velocity` (static) | v2 | `trajectory run-waypoints`（同一命令） | `RunJointTrajectory`（同一 rpc） | B | waypoint 带 `velocity` 字段时走这条分支；由请求里每个 `WaypointSpec` 是否填 `velocity` 决定服务端选择哪种插值，无需拆两个命令 |

### 2.9 示教录制回放（4 项，均 v2）

| # | SDK 方法 | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| 38 | `Recorder.__init__` | v2（不单独暴露构造） | `teach record start` | `TeachRecordStart` | 立即返回 | 由 rpc 内部实例化，不作为独立可调用能力 |
| 39 | `Recorder.log` | v2（不单独暴露） | （无对应 CLI，自动执行） | — | E | HardwareLoop 每周期自动调用，非逐次 RPC；`teach record start/stop` 只是开关标志位 |
| 40 | `Recorder.close` | v2（不单独暴露） | `teach record stop` | `TeachRecordStop` | 立即返回 | rpc 内部调用，返回保存路径+帧数 |
| 41 | `Recorder.play` (static) | v2 | `teach play` | `TeachPlay` | B | 起点移动阶段（含夹爪 2s 硬编码 sleep）+ 整段回放都耗时不定，必须异步；`CancelExecution` 需定义"回放中途取消"的安全收尾策略（减速停止而非硬切） |

### 2.10 生命周期（不暴露）

| # | SDK 方法 | 阶段 | 理由 |
|---|---|---|---|
| 42 | `Panthera.__init__` | 不暴露 | armd 启动时调用一次、`config_path` 固定为部署配置的一部分，不是按次调用的用户能力；切配置走 armd 部署/重启，不进契约 |

**2.1–2.10 小计**：42 个 SDK 方法/静态方法，其中 CLI+rpc 映射 33 项（v1 20 项 / v2 13 项，按"能力"计，教学录制的 3 项 #38-40 算作 1 项可操作能力"teach record"），显式不暴露 8 项（均给出理由），生命周期不暴露 1 项。**0 项遗漏、0 项无理由**。

### 2.11 补充映射：架构必需但不在给定清单内的继承层方法

架构文档明确要求 EStop 直通与回零，但对应方法（`set_stop()` / `set_reset_zero()`）来自 `htr.Robot` 父类，不在任务给定的 42 方法清单里，真实签名未核实（见第 7 章）。仍需在 v1 落地，故单列：

| # | 方法（继承层，签名待核实） | 阶段 | CLI | rpc | 模式 | 备注 |
|---|---|---|---|---|---|---|
| S1 | `set_stop()` | v1 | `estop trigger` | `EStop` | 立即返回，最高优先级直通（绕过排队） | EStop 必须在 gRPC asyncio 层之前/旁路特殊处理，不与其它指令排队 |
| S2 | `set_reset_zero()` | v1 | `calibrate zero` | `SetZero` | A 或 B（取决于回零耗时，待核实） | 危险操作：机械臂会运动到零位，CLI 端要求 `--confirm` 或交互确认 |

电机层更底层的方法（`motor_send_cmd`、`send_get_motor_state_cmd`、`get_motors`、`Motors[i].*` 等）已被 `Panthera` 类的 `get_current_*`/`check_position_reached` 等高阶方法完整封装，**不单独暴露**：直接暴露电机层会绕过 `Panthera` 自带的限位检查与状态缓存逻辑，存在打破安全层的风险。若未来确有电机级诊断需求（固件版本、使能状态排查等），应先核对 `.pyi`/`bindings.cpp`，再单独评估是否新增 `panthera motor debug` 诊断命令；本计划不预先设计。

---

## 3. panthera-cli 完整命令树（typer 风格，短横线子命令）

### 3.1 v1（24 条命令）

```
panthera control acquire [--client-id TEXT] [--force]
    示例: panthera control acquire --client-id wpf-main

panthera control release
panthera control status
    示例: panthera control status --json

panthera estop trigger [--reason TEXT]
    示例: panthera estop trigger --reason "手动急停"
panthera estop reset [--confirm]

panthera safety check-reached --target FLOAT_LIST [--tolerance FLOAT=0.1]
    示例: panthera safety check-reached --target 0,0,0,0,0,0 --tolerance 0.05
panthera safety limits show [--json]

panthera calibrate zero [--confirm]
    示例: panthera calibrate zero --confirm

panthera state get [--joints/--no-joints] [--gripper/--no-gripper] [--json]
    示例: panthera state get --json
panthera state watch [--rate-hz INT=10] [--joints/--no-joints] [--gripper/--no-gripper]
    示例: panthera state watch --rate-hz 20

panthera joint jog --vel FLOAT_LIST [--duration FLOAT] [--interactive]
    示例: panthera joint jog --vel 0,0.2,0,0,0,0 --duration 3.0
panthera joint move --pos FLOAT_LIST --vel FLOAT_LIST [--max-torque FLOAT_LIST] [--wait] [--tolerance FLOAT=0.1] [--timeout FLOAT=15.0]
    示例: panthera joint move --pos 0,0,0,0,0,0 --vel 0.3,0.3,0.3,0.3,0.3,0.3 --wait
panthera joint movej --pos FLOAT_LIST --duration FLOAT [--max-torque FLOAT_LIST] [--wait] [--timeout FLOAT=15.0]
    示例: panthera joint movej --pos 0.1,0.2,0,0,0,0 --duration 3.0 --wait

panthera gripper open [--pos FLOAT=1.6] [--vel FLOAT=0.5] [--max-torque FLOAT=0.5]
panthera gripper close [--pos FLOAT=0.0] [--vel FLOAT=0.5] [--max-torque FLOAT=0.5]
panthera gripper move --pos FLOAT --vel FLOAT [--max-torque FLOAT=0.5]

panthera kinematics fk [--joint-angles FLOAT_LIST]
panthera kinematics ik --pos FLOAT_LIST [--rpy FLOAT_LIST] [--init-q FLOAT_LIST] [--single-init/--multi-init] [--num-attempts INT=8]
    示例: panthera kinematics ik --pos 0.3,0.0,0.4 --rpy 0,1.57,0
panthera kinematics jacobian [--joint-angles FLOAT_LIST]
panthera kinematics manipulability [--joint-angles FLOAT_LIST]

panthera cartesian movel --pos FLOAT_LIST [--rpy FLOAT_LIST] [--duration FLOAT] [--no-spline] [--max-torque FLOAT_LIST]
    示例: panthera cartesian movel --pos 0.35,0.1,0.3 --rpy 0,3.14,0 --duration 4.0
panthera cartesian plan-preview --waypoints TEXT [--avoid-collisions]
    示例: panthera cartesian plan-preview --waypoints "0.3,0,0.3,0,0,0;0.35,0.1,0.3,0,0,0"

panthera daemon status
panthera daemon version
```

### 3.2 v2（18 条命令）

```
panthera joint mit --pos FLOAT_LIST --vel FLOAT_LIST --tqe FLOAT_LIST --kp FLOAT_LIST --kd FLOAT_LIST [--stream FILE]
panthera gripper mit --pos FLOAT --vel FLOAT --tqe FLOAT --kp FLOAT --kd FLOAT

panthera cartesian jog [--linear-vel FLOAT_LIST] [--angular-vel FLOAT_LIST] [--damping FLOAT=0.01] [--interactive]
    示例: panthera cartesian jog --linear-vel 0.05,0,0 --interactive

panthera dynamics gravity [--joint-angles FLOAT_LIST]
panthera dynamics coriolis [--joint-angles FLOAT_LIST] [--joint-vel FLOAT_LIST]
panthera dynamics mass-matrix [--joint-angles FLOAT_LIST]
panthera dynamics inertia [--joint-angles FLOAT_LIST] [--accel FLOAT_LIST]
panthera dynamics inverse [--joint-angles FLOAT_LIST] [--joint-vel FLOAT_LIST] [--accel FLOAT_LIST]
panthera dynamics friction --vel FLOAT_LIST [--fc FLOAT_LIST] [--fv FLOAT_LIST] [--vel-threshold FLOAT=0.01]

panthera trajectory run-waypoints --waypoints-file PATH [--durations FLOAT_LIST]

panthera teach start [--kp FLOAT_LIST] [--kd FLOAT_LIST]
panthera teach stop
panthera teach record start [--path PATH] [--flush-interval FLOAT=0.2]
panthera teach record stop
panthera teach play PATH [--kp FLOAT_LIST] [--kd FLOAT_LIST] [--mode mit|posvel] [--playback-dt FLOAT=0.01] [--smooth-window INT=7]
panthera teach list [--json]

panthera camera stream [--encode h264]                         # 占位，详见 camera.proto（未来）
panthera dataset export-lerobot --traj PATH --out DIR           # 占位，详见 dataset.proto（未来）
```

---

## 4. arm.proto 全量草案

```proto
syntax = "proto3";
package panthera.arm.v1;

// ============ 通用 ============
message Empty {}

enum ExecState {
  EXEC_STATE_UNSPECIFIED = 0;
  RUNNING = 1;
  DONE = 2;
  FAILED = 3;
  CANCELLED = 4;
}

message ExecutionAccepted { string execution_id = 1; }

message StreamExecutionRequest { string execution_id = 1; }

message ExecutionStatus {
  string execution_id = 1;
  ExecState state = 2;
  double fraction = 3;              // 0.0-1.0
  JointState joint_state = 4;       // 可选，便于观察实时进度
  string error_message = 5;         // state == FAILED 时填充
}

message CancelExecutionRequest { string execution_id = 1; }
message CancelExecutionResponse { bool cancelled = 1; }

// ============ 控制权 / 安全层 ============
message AcquireControlRequest { string client_id = 1; bool force = 2; }
message AcquireControlResponse { bool granted = 1; string holder_client_id = 2; string lease_token = 3; }
message ReleaseControlRequest { string lease_token = 1; }

message ControlStatus {
  bool held = 1;
  string holder_client_id = 2;
  bool estop_engaged = 3;
  bool watchdog_ok = 4;
  int64 last_heartbeat_age_ms = 5;
}

message HeartbeatRequest { string lease_token = 1; }
message HeartbeatResponse { bool ok = 1; int64 server_time_ms = 2; }

message EStopRequest { string reason = 1; }
message EStopResponse { bool engaged = 1; int64 timestamp_ms = 2; }

message JointLimit { string name = 1; double pos_min = 2; double pos_max = 3; double vel_max = 4; double torque_max = 5; }
message GripperLimit { double pos_min = 1; double pos_max = 2; double vel_max = 3; double torque_max = 4; }
message SoftLimits { repeated JointLimit joint_limits = 1; GripperLimit gripper_limit = 2; }

message SetZeroRequest { bool confirm = 1; }
message SetZeroResponse { bool accepted = 1; }

// ============ 状态查询 ============
message MotorState {
  // 字段为草案，需在实现阶段核对 SDK 实际电机状态对象补全
  string name = 1;
  int32 motor_id = 2;
  double position = 3;
  double velocity = 4;
  double torque = 5;
  bool online = 6;
}

message JointState { repeated MotorState joints = 1; int64 timestamp_ms = 2; }
message GripperState { MotorState state = 1; int64 timestamp_ms = 2; }
message RobotState { JointState joint = 1; GripperState gripper = 2; }
message StreamStateRequest { double rate_hz = 1; bool joints = 2; bool gripper = 3; }

message CheckReachedRequest { repeated double target_positions = 1; double tolerance = 2; }
message CheckReachedResponse { bool reached = 1; repeated double errors = 2; }

// ============ 关节控制 ============
message JointMoveRequest {
  repeated double positions = 1;
  repeated double velocities = 2;
  repeated double max_torque = 3;   // 可选，长度0表示不限
  bool wait = 4;
  double tolerance = 5;
  double timeout_s = 6;
}
message JointMoveResponse { bool accepted = 1; bool reached = 2; repeated double errors = 3; string reject_reason = 4; }

message JointJogCommand { repeated double velocities = 1; }
message JointJogFeedback { JointState joint_state = 1; repeated bool limit_hit = 2; }

message MoveJRequest {
  repeated double positions = 1;
  double duration_s = 2;
  repeated double max_torque = 3;
  bool wait = 4;
  double tolerance = 5;
  double timeout_s = 6;
}
message MoveJResponse { bool accepted = 1; bool reached = 2; repeated double errors = 3; string reject_reason = 4; }

// v2：MIT 阻抗模式
message JointMITCommand {
  repeated double positions = 1;
  repeated double velocities = 2;
  repeated double torques = 3;
  repeated double kp = 4;
  repeated double kd = 5;
}
message JointMITFeedback { JointState joint_state = 1; }

// ============ 夹爪控制 ============
message GripperMoveRequest { double position = 1; double velocity = 2; double max_torque = 3; }
message GripperMoveResponse { bool accepted = 1; }
message GripperOpenRequest { double position = 1; double velocity = 2; double max_torque = 3; }
message GripperCloseRequest { double position = 1; double velocity = 2; double max_torque = 3; }

// v2
message GripperMITCommand { double position = 1; double velocity = 2; double torque = 3; double kp = 4; double kd = 5; }
message GripperMITResponse { bool accepted = 1; }

// ============ 运动学 ============
message JointAnglesOptional { repeated double joint_angles = 1; } // 空=使用当前位置

message ForwardKinematicsResponse {
  repeated double position = 1;        // len 3
  repeated double rotation_matrix = 2; // len 9, row-major
  repeated double transform = 3;       // len 16, row-major 4x4
  repeated double used_joint_angles = 4;
}

message JacobianResponse { repeated double matrix = 1; int32 rows = 2; int32 cols = 3; } // row-major rows x cols
message ManipulabilityResponse { double mu = 1; }

// 姿态：允许旋转矩阵或欧拉角二选一（服务端用 rotation_matrix_from_euler 编码欧拉角）
message CartesianPose {
  repeated double position = 1;        // len 3
  oneof orientation {
    RPY rpy = 2;
    RotationMatrix matrix = 3;
  }
}
message RPY { double roll = 1; double pitch = 2; double yaw = 3; }
message RotationMatrix { repeated double values = 1; } // len 9

message InverseKinematicsRequest {
  CartesianPose target = 1;
  repeated double init_q = 2;
  int32 max_iter = 3;         // default 1000
  double eps = 4;             // default 1e-3
  double damping = 5;         // default 1e-2
  bool adaptive_damping = 6;  // default true
  bool multi_init = 7;        // default true
  int32 num_attempts = 8;     // default 8
}
message InverseKinematicsResponse { bool found = 1; repeated double joint_angles = 2; double error = 3; }

// ============ 笛卡尔运动 ============
message PlanCartesianPathRequest { repeated CartesianPose waypoints = 1; bool avoid_collisions = 2; }
message JointTrajectoryPoint { repeated double positions = 1; repeated double velocities = 2; double timestamp_s = 3; }
message PlanCartesianPathResponse { repeated JointTrajectoryPoint joint_trajectory = 1; double fraction = 2; }

message MoveLRequest {
  CartesianPose target = 1;
  double duration_s = 2;      // 可选，省略则按内部估算
  bool use_spline = 3;        // default true
  repeated double max_torque = 4;
}

// v2：笛卡尔速度 jog（Jacobian + 阻尼伪逆，服务端内部实现，不单独暴露 rpc）
message CartesianJogCommand { repeated double linear_velocity = 1; repeated double angular_velocity = 2; double damping = 3; }
message CartesianJogFeedback { JointState joint_state = 1; double manipulability = 2; }

// ============ 动力学（v2） ============
enum DynamicsTerm {
  DYNAMICS_TERM_UNSPECIFIED = 0;
  GRAVITY = 1;
  CORIOLIS = 2;
  MASS_MATRIX = 3;
  INERTIA = 4;
  FULL_INVERSE_DYNAMICS = 5;
  FRICTION = 6;
}
message DynamicsQueryRequest {
  DynamicsTerm term = 1;
  repeated double q = 2;
  repeated double v = 3;
  repeated double a = 4;
  repeated double fc = 5;            // FRICTION 用；armd 未提供时用配置默认值兜底
  repeated double fv = 6;
  double vel_threshold = 7;          // default 0.01
}
message DynamicsQueryResponse {
  repeated double gravity = 1;
  repeated double coriolis_matrix = 2;   // NxN row-major
  repeated double coriolis_vector = 3;
  repeated double mass_matrix = 4;       // NxN row-major
  repeated double inertia_terms = 5;
  repeated double inverse_dynamics = 6;
  repeated double friction_compensation = 7;
}

// ============ 多点轨迹（v2） ============
message WaypointSpec { repeated double positions = 1; repeated double velocities = 2; } // velocities 为空=零边界速度插值
message RunJointTrajectoryRequest { repeated WaypointSpec waypoints = 1; repeated double durations = 2; }

// ============ 示教录制回放（v2） ============
message TeachStartRequest { repeated double kp = 1; repeated double kd = 2; }
message TeachStartResponse { bool accepted = 1; }
message TeachStopResponse { bool accepted = 1; }

message TeachRecordStartRequest { string path = 1; double flush_interval = 2; }
message TeachRecordStartResponse { bool accepted = 1; string path = 2; }
message TeachRecordStopResponse { bool accepted = 1; string saved_path = 2; int64 frame_count = 3; }

enum PlaybackMode { PLAYBACK_MODE_UNSPECIFIED = 0; MIT = 1; POSVEL = 2; }
message TeachPlayRequest {
  string path = 1;
  repeated double kp = 2;
  repeated double kd = 3;
  repeated double fc = 4;
  repeated double fv = 5;
  double vel_threshold = 6;
  repeated double tau_limit = 7;
  double gripper_kp = 8;
  double gripper_kd = 9;
  double playback_dt = 10;
  int32 smooth_window = 11;
  PlaybackMode mode = 12;
}

message TeachFileInfo { string path = 1; int64 recorded_at = 2; double duration_s = 3; int64 frame_count = 4; }
message TeachListResponse { repeated TeachFileInfo files = 1; }

// ============ 占位（v2 路线图，非 SDK 方法，详见独立 proto） ============
message CameraStreamRequest { string encode = 1; }
message DatasetExportRequest { string traj_path = 1; string out_dir = 2; }

// ============ Service ============
service ArmService {
  // 控制权 / 安全层
  rpc AcquireControl(AcquireControlRequest) returns (AcquireControlResponse);
  rpc ReleaseControl(ReleaseControlRequest) returns (Empty);
  rpc GetControlStatus(Empty) returns (ControlStatus);
  rpc Heartbeat(stream HeartbeatRequest) returns (stream HeartbeatResponse);
  rpc EStop(EStopRequest) returns (EStopResponse);
  rpc ClearEStop(Empty) returns (EStopResponse);
  rpc GetSoftLimits(Empty) returns (SoftLimits);
  rpc SetZero(SetZeroRequest) returns (SetZeroResponse);

  // 状态
  rpc GetJointState(Empty) returns (JointState);
  rpc GetGripperState(Empty) returns (GripperState);
  rpc StreamState(StreamStateRequest) returns (stream RobotState);
  rpc CheckReached(CheckReachedRequest) returns (CheckReachedResponse);

  // 关节控制（v1）
  rpc JointMove(JointMoveRequest) returns (JointMoveResponse);
  rpc JointJog(stream JointJogCommand) returns (stream JointJogFeedback);
  rpc MoveJ(MoveJRequest) returns (MoveJResponse);

  // 关节控制（v2）
  rpc JointMIT(stream JointMITCommand) returns (stream JointMITFeedback);

  // 夹爪（v1）
  rpc GripperMove(GripperMoveRequest) returns (GripperMoveResponse);
  rpc GripperOpen(GripperOpenRequest) returns (GripperMoveResponse);
  rpc GripperClose(GripperCloseRequest) returns (GripperMoveResponse);
  // 夹爪（v2）
  rpc GripperMIT(GripperMITCommand) returns (GripperMITResponse);

  // 运动学（v1）
  rpc GetForwardKinematics(JointAnglesOptional) returns (ForwardKinematicsResponse);
  rpc GetJacobian(JointAnglesOptional) returns (JacobianResponse);
  rpc GetManipulability(JointAnglesOptional) returns (ManipulabilityResponse);
  rpc GetInverseKinematics(InverseKinematicsRequest) returns (InverseKinematicsResponse);

  // 笛卡尔（v1）
  rpc PlanCartesianPath(PlanCartesianPathRequest) returns (PlanCartesianPathResponse);
  rpc MoveL(MoveLRequest) returns (ExecutionAccepted);
  // 笛卡尔（v2）
  rpc CartesianJog(stream CartesianJogCommand) returns (stream CartesianJogFeedback);

  // 动力学（v2）
  rpc GetDynamicsTerm(DynamicsQueryRequest) returns (DynamicsQueryResponse);

  // 多点轨迹（v2）
  rpc RunJointTrajectory(RunJointTrajectoryRequest) returns (ExecutionAccepted);

  // 示教录制回放（v2）
  rpc TeachStart(TeachStartRequest) returns (TeachStartResponse);
  rpc TeachStop(Empty) returns (TeachStopResponse);
  rpc TeachRecordStart(TeachRecordStartRequest) returns (TeachRecordStartResponse);
  rpc TeachRecordStop(Empty) returns (TeachRecordStopResponse);
  rpc TeachPlay(TeachPlayRequest) returns (ExecutionAccepted);
  rpc TeachList(Empty) returns (TeachListResponse);

  // 通用异步执行观察（moveL / trajectory / teach play 共用）
  rpc StreamExecution(StreamExecutionRequest) returns (stream ExecutionStatus);
  rpc CancelExecution(CancelExecutionRequest) returns (CancelExecutionResponse);

  // 占位（详见未来 camera.proto / dataset.proto，此处仅保留 CLI 入口的最小桩）
  rpc StreamCamera(CameraStreamRequest) returns (stream Empty);
  rpc ExportLeRobotDataset(DatasetExportRequest) returns (ExecutionAccepted);
}
```

---

## 5. v1 / v2 阶段划分总结

| 阶段 | 范围 | CLI 命令数 | 对应 SDK 能力 |
|---|---|---|---|
| **v1** | 监控 + jog + moveJ/moveL + 夹爪 + 安全（含 EStop/回零） | **24** | #1-11,13,15-21,23,25,28 + S1,S2 |
| **v2** | 阻抗/MIT + 动力学诊断 + 多点轨迹 + 拖动示教录制回放 + 相机/LeRobot占位 | **18** | #12,14,29-41 + 占位2项 |
| 不暴露（有理由） | 内部实现细节 / 纯工具函数 / 已被封装 | 8 | #18,22,24,26,27,+电机层(说明性,非计数) |
| 生命周期 | 构造函数 | 1 | #42 |

**合计 42 项 SDK 能力，24+18=42 条 CLI 命令覆盖 33 项直接能力（8 项不暴露+1 项生命周期均有理由），核对完整、无遗漏。**

---

## 6. 里程碑与验收标准

### v1

- **M1 安全骨架**：`control acquire/release/status`、`estop trigger/reset`、`safety limits show`、Heartbeat/watchdog 打通。
  验收：① 两个客户端并发 `acquire`，第二个被拒绝且能看到当前持有者 `client_id`；② 主动停止心跳超过 watchdog 阈值后 armd 自动 `release` 并将关节速度指令置零；③ `estop trigger` 后 100ms 内下发的 `JointMove`/`MoveJ`/`MoveL` 一律返回 `REJECTED`（而非静默失败或连接中断），`estop reset` 后恢复正常。
- **M2 状态与标定**：`state get/watch`、`calibrate zero`。
  验收：`state watch --rate-hz 10` 能连续输出且断线重连不 crash；`calibrate zero --confirm` 后 `state get` 显示各关节位置为零位。
- **M3 关节/夹爪控制**：`joint jog/move/movej`、`gripper open/close/move`。
  验收：越限位的 `joint move` 被拒绝，错误信息含关节名+超限方向+限位值；`joint movej --wait` 在到达或超时后才返回，误差在 `--tolerance` 内；`joint jog` 流关闭或 250ms 无新指令后机械臂自动停止（不漂移）；`gripper open/close` 能正确反映限位拒绝（而非 SDK 原生的恒 `None`）。
- **M4 笛卡尔与运动学**：`kinematics fk/ik/jacobian/manipulability`、`cartesian movel/plan-preview`、`safety check-reached`。
  验收：`plan-preview` 对不可达路径返回 `fraction<1` 且不执行任何运动；`movel` 执行期间 `StreamExecution` 能看到 `fraction` 单调递增，`DONE/FAILED/CANCELLED` 三态清晰区分；`ik` 对不可达目标返回 `found=false`，不抛异常穿透到 gRPC。

  **v1 验收总口径**：24 条命令全部可用，且上述 4 组场景全部通过后，v1 视为完成。

### v2

- **M5 阻抗/动力学**：`joint mit/gripper mit`、`dynamics *`（6条）。
  验收：`dynamics` 各项在给定 `q/v/a` 下的数值与直接调用 SDK 对拍一致；`dynamics friction` 未提供 `--fc/--fv` 时使用配置默认值兜底，不抛异常。
- **M6 多点轨迹**：`trajectory run-waypoints`。
  验收：给定含/不含中间速度的 waypoint 列表，服务端正确选择两种插值分支；执行状态可流式观察、可取消。
- **M7 拖动示教录制回放**：`teach start/stop/record start/record stop/play/list`。
  验收：`teach start` 后徒手拖动阻力明显降低（重力+摩擦补偿生效）；录制的 jsonl 字段与 `Recorder.log` 一致；`teach play` 能回放且末端误差在可接受范围内；回放中途 `CancelExecution` 能安全收尾（明确定义的减速停止策略，而非让机械臂悬停在危险位置）。
- **M8 相机流与 LeRobot 导出（占位落地）**：`camera stream`、`dataset export-lerobot`。
  验收：本阶段只要求接口占位打通与"录制轨迹→LeRobot 字段映射"草图明确；具体验收标准留给独立的 `camera.proto`/`dataset.proto` 计划。
- **M9 无损审计收尾**：核对 `.pyi`/`bindings.cpp` 确认 `set_stop`/`set_reset_zero`/电机层方法的真实签名，更新第 2.11 节为最终版；逐行核对"方法清单 vs 已实现 rpc"，确保 0 条遗漏、0 条无理由。

---

## 7. 已知缺口与后续行动项

1. **继承层签名未核实**（第 2.11 节 S1/S2）：`set_stop()`/`set_reset_zero()` 来自 `htr.Robot`，需要核对 `.pyi` 桩文件或 `src/bindings.cpp` 才能确认参数与返回值，当前 `EStop`/`SetZero` 的 request/response 字段是按"最小可用"猜测的占位设计。
2. **`MotorState` 字段草案未核实**：`get_current_state` 返回的电机状态对象具体字段（是否含温度/电压/使能位等）未知，`arm.proto` 里的 `MotorState` 是保守占位，实现阶段需要对照真实返回对象补全或裁剪。
3. **D405 视频流 / LeRobot 导出不在本次核对范围**：它们不是 `Panthera`/`Recorder` 的方法，是 README 的 v2 路线图条目；`arm.proto` 仅留最小占位 rpc，正式契约建议独立成文件，不与本次"无损"核对的 42 项 SDK 清单混计。
4. **`moveL`/`teach play` 取消时的安全收尾策略未定义**：`CancelExecution` 在轨迹执行到一半时如何安全停止（原地悬停 vs 减速停止 vs 回到最近安全点）需要在 M4/M7 实现前单独设计，本计划只标注了"需要"，未给出算法。
