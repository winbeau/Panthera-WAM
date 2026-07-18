# Panthera-WAM 实施进度看板

> **唯一进度来源**。与 `FINAL_PLAN.md` 配套：FINAL_PLAN 定「做什么 / 为什么」，本文件跟踪「做到哪了」。
> 每完成一项即打勾并 `commit` + `push`，提交信息中文、前缀 `feat:/fix:/docs:/test:/chore:`。
> 与 FINAL_PLAN 冲突时，**以 FINAL_PLAN 为准**，并回来修正本文件。

## 图例

| 记号 | 含义 |
|---|---|
| `[ ]` / `[x]` | 未完成 / 已完成 |
| 🧪 | 可用 `armd --sim` 或纯计算完成，**无需真机**，可自主推进 |
| 🔒 | **需真机 + 用户在场逐次确认**（安全红线，不可自动执行） |
| ⛓ | 前置依赖未满足，被阻塞 |

> **安全约束（不可绕过）**：一切 🔒 项目在用户明确到场确认前不得执行。日常开发一律走 `armd --sim`。
> 首次真机顺序：读状态 → Enable → 单关节 ≤5° jog → EStop 演练 → 才允许 moveJ/moveL。

## 进度总览

| 阶段 | 范围 | 进度 |
|---|---|---|
| 阶段 -1 | 前置准备（环境 + SDK 核实） | 5 / 5 ✅ |
| 阶段 0 | M0 架构验证 spike（v1 硬前置） | 0 / 6 |
| 阶段 1 | 契约与仓库骨架 | 0 / 6 |
| 阶段 2 | v1：armd + CLI（M1–M4） | 0 / 4 |
| 阶段 3 | WPF v1（M-W0 – M-W3） | 0 / 5 |
| 阶段 4 | v2（M5–M9） | 0 / 5 |

---

## 阶段 -1：前置准备 ✅

- [x] 切出实施分支 `feat/foundation-m0`
- [x] 克隆官方 SDK（wsl-host `~/Panthera-HT_SDK` + VPS 只读副本，均在仓库外）
- [x] wsl-host 建 `uv` 环境 `~/panthera-wam-env`：`pin 2.7.0 / numpy 1.26.4 / scipy 1.15.3 / pyyaml 6.0.3 / hightorque_robot 1.2.0`，`import hightorque_robot` 通过
- [x] §8「实现前必须核实」4 项全部结案，回写 FINAL_PLAN「SDK 源码核实结论」
- [x] WPF USB/WSL 环境引导纳入计划（WPF_PLAN §3.7 + M-W0.5）

---

## 阶段 0：M0 架构验证 spike

> **硬约束：M0 全过才允许开工 v1。** 其中 M0-1/M0-2 必须真机。

- [ ] 🧪 **N7 核实**：读 `canport.cpp` / `canboard.cpp` 组帧与发送逻辑，确认「6 关节 + 夹爪共用一帧 CAN TX、帧头单一 `cmd` 模式」是否成立 → 决定 HardwareLoop 能否在同一周期混用控制模式。结论回写 FINAL_PLAN §V6-N7
- [ ] 🧪 **M0 spike 脚本**：编写 `spikes/m0_1_estop_preempt.py` / `m0_2_movel_loop.py` / `m0_3_compute_split.py`（真机脚本须先打印动作再二次确认）
- [ ] 🧪 **M0-3 计算/IO 分流实测**：双 pinocchio 实例独立性 + `multi_init` IK 墙钟耗时 + 对控制周期抖动影响；产出「独立实例」或「marshal+超时预算」二选一结论
- [ ] 🔒 **M0-1 非阻塞循环 + 可抢占 EStop**：逐周期步进 `joint move`，中途置 estop 标志，实测 `set_stop` 在下一周期生效、总延迟 < 100ms
- [ ] 🔒 **M0-2 自建 moveL 执行循环**：`compute_cartesian_path` + 逐点 `pos_vel_tqe_kp_kd` 复现直线，`fraction` 单调、中途可取消，末端误差与 SDK `moveL()` 对拍一致
- [ ] **M0 收口**：锁定控制周期频率（目标 200–500Hz，受电机通信上限约束），结论回写 FINAL_PLAN §8-7；确认 M0 三项全过

---

## 阶段 1：契约与仓库骨架

- [ ] 🧪 **`proto/arm.proto`**：按 FINAL_PLAN §7 + 核实结论落地。必须包含的修正：`MotorState` 删 `online`、补 `fault`/`mode`/`time`；IK optional 默认值保持不变（已核实正确）；`avoid_collisions` 标注未实现
- [ ] 🧪 **codegen**：Python stub（armd/cli）+ C# stub（wpf）生成脚本，两端同源、一起提交
- [ ] 🧪 **仓库骨架**：`proto/ armd/ cli/ wpf/ deploy/`，armd 与 cli 建为 `uv` 工程（Python 3.10+，类型标注）
- [ ] 🧪 **`armd --sim` 仿真后端**：不依赖真机的 `Panthera` 替身，支持全部只读 + 运动语义，供全部 pytest 使用
- [ ] 🧪 **HardwareLoop 骨架**：单线程独占、逐周期非阻塞步进、estop/cancel 标志优先检查（按 N7 结论确定下发编排）
- [ ] 🧪 **测试与 CI**：pytest 全走 `--sim`；`make`/脚本一键跑通

---

## 阶段 2：v1 —— armd + panthera-cli（24 条命令）

### M1 安全骨架
- [ ] 🧪 实现：`control acquire/release/status`、metadata lease 拦截器、`estop trigger/reset`、`safety limits show`、Heartbeat/watchdog
- [ ] 🧪 验收①：两客户端并发 acquire，第二个被拒且能看到持有者 `client_id`
- [ ] 🧪 验收②：非持锁客户端调 `JointMove` 被 `PERMISSION_DENIED` 拒绝
- [ ] 🧪 验收③：watchdog 超时自动 release，并**按当前控制模式**正确停止（位置模式保位 / 速度模式归零）
- [ ] 🔒 验收④：`estop trigger` 后运动类 RPC 一律 REJECTED，实测抢占延迟 < 100ms，`estop reset --confirm` 后恢复
- [ ] 🧪 **N4 防护**：检测 SDK 串口重连导致 `Motors` 缓存失效并自动重取（避免悬垂引用）
- [ ] **N1 回归检查**：启动自检记录 SDK 版本，断言 `detect_motor_limit()` 仍未接入调用链（否则 EStop 可靠性需重评）
- [ ] **N2 决策**：是否启用电机固件看门狗 `motor_timeout_ms`（当前为 0=禁用），定值并写入部署配置

### M2 状态与标定
- [ ] 🧪 实现：`state get`、`state watch`（`StreamState`）、`calibrate zero`
- [ ] 🧪 验收：`state watch` 连续输出、断线重连不 crash、`age_ms` 正确反映新鲜度
- [ ] 🧪 **999.0 哨兵处理**：`position == 999.0` 视为未连接/无效，不得当真实位置推给客户端
- [ ] 🔒 验收：`calibrate zero --confirm` 行为与核实结论一致（不产生运动、需补 `motor_send_cmd()`、全体归零不持久化）

### M3 关节 / 夹爪控制
- [ ] 🧪 实现：`joint jog/move/movej`、`gripper open/close/move`（gripper 直调 `gripper_control` 绕开恒 `None` 缺陷）
- [ ] 🧪 验收：越限 `joint move` 被拒且含关节名 + 方向 + 限位值
- [ ] 🧪 验收：`joint jog` 关流或 250ms 无新指令自动停、不漂移
- [ ] 🔒 验收：`joint movej --wait` 到达/超时才返回、误差在 `--tolerance` 内，且等待期间 watchdog/EStop 仍响应
- [ ] 🔒 验收：`gripper open/close` 正确反映限位拒绝并带 `reject_reason`

### M4 笛卡尔与运动学
- [ ] 🧪 实现：`kinematics fk/ik/jacobian/manipulability`、`cartesian movel/plan-preview`、`safety check-reached`
- [ ] 🧪 验收：`plan-preview` 对不可达路径返回 `fraction<1` 且不执行运动；无 avoid-collisions 误导
- [ ] 🧪 验收：`ik` 不可达返回 `found=false` 不穿透异常，超时返回 `timeout=true`
- [ ] **安全收尾算法定案**（FINAL_PLAN §8-6）：`CancelExecution`/watchdog 触发时的减速策略
- [ ] 🔒 验收：`movel` 期间 `StreamExecution` 见 `fraction` 单调递增，`DONE/FAILED/CANCELLED` 三态清晰，取消能减速收尾
- [ ] **v1 完成口径**：24 条命令可用 + M1–M4 场景全过

---

## 阶段 3：WPF v1（.NET 9 Fluent，视觉基准＝稿 C 驾驶舱）

- [ ] **M-W0 脚手架**：解决方案分层、DI/Host、`ThemeMode` 三态 POC、`IArmdClient` + 最小 gRPC 连接；csproj 压制 `WPF0001`
- [ ] **M-W0.5 环境引导**（WPF_PLAN §3.7）：usbipd 检测/bind/attach（按 `VID_CAF1:FFFF`+序列号匹配，不硬编码 busid）、WSL 拉起、`/dev/ttyACM*` ≥4 校验、armd 启动探活；一次性提权、命令可见、全程写日志
- [ ] **M-W1 只读监控**：关节圆形仪表 + 中央雷达俯视图 + `StreamState` 30fps 节流管线（无需 lease）
- [ ] **M-W2 控制闭环**：获取控制权 + jog pod + `joint move/movej` + 夹爪 + 常驻 EStop + moveL 进度/取消
- [ ] **M-W3 主题打磨**：系统/浅色/深色三态 + 高对比校验；扫描线/发光等装饰；键盘可达性

---

## 阶段 4：v2

- [ ] **M5 阻抗/动力学**：`joint mit`/`gripper mit`、`dynamics *`（6 条）。`friction` 缺 `--fc/--fv` 用配置默认兜底不抛异常（默认值取核实结论 §V5 参考系数）
- [ ] **M6 多点轨迹**：`trajectory run-waypoints`（自建执行循环，`Joint_Pos_Vel` 而非 MIT）
- [ ] **M7 拖动示教录制回放**：`teach start/stop/record*/play/list`（自由拖动＝kp/kd 全零 + 重力/摩擦前馈）
- [ ] **M8 相机流 + LeRobot 导出（占位落地）**：`camera stream`、`dataset export-lerobot`
- [ ] **M9 无损审计收尾**：逐行核对 42 项方法清单 vs 已实现 rpc，0 遗漏、0 无理由

---

## 待用户决策 / 需真机的挂起项

| 项 | 需要什么 |
|---|---|
| M0-1 / M0-2 | 机械臂挂进 WSL（`usbipd attach --wsl --busid 3-2`，管理员 PowerShell）+ 用户在场 |
| 所有 🔒 验收项 | 用户当次明确确认 |
| WPF 构建与运行 | Windows 主机 `windows-host`（`ssh <windows-user>@<WINDOWS_HOST_IP>`），不可在 WSL 内跑 exe |
| N2（`motor_timeout_ms`） | 用户确认是否启用硬件看门狗及取值 |
