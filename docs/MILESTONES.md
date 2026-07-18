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
| 阶段 0 | M0 架构验证 spike（v1 硬前置） | 6 / 6 ✅ |
| 阶段 1 | 契约与仓库骨架 | 6 / 6 ✅ |
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

## 阶段 0：M0 架构验证 spike ✅

> **硬约束：M0 全过才允许开工 v1。** 其中 M0-1/M0-2 必须真机。

- [x] 🧪 **N7 核实** ✅ **成立**：整帧单模式，切模式会把同端口其它电机槽位抹成 `0x8000`（无指令哨兵）。7 个电机同在一个 CANport，故**关节与夹爪不可跨模式共存于同一周期**。armd 规则：每周期用同一模式写满 7 槽再发一次；夹爪指令表达成关节当前模式。结论已回写 FINAL_PLAN §V6-N7
- [x] 🧪 **M0 spike 脚本** ✅ 三份齐备：`m0_common.py` / `m0_3_compute_split.py` / `m0_1_estop_preempt.py` / `m0_2_movel_loop.py`，已同步至 wsl-host `~/panthera-wam/spikes/m0/` 并通过 import 冒烟。两份真机脚本均走 `confirm_motion()`（先打印全部动作 → 要求输入 `YES`）、幅度硬上限（M0-1 ≤15°、M0-2 ≤10cm）、保守力矩、`finally` 无条件 `set_stop()`
- [x] 🧪 **M0-3 计算/IO 分流实测** ✅ **通过**：双实例安全独立；IK 可达 p50=2.2ms、不可达最坏 170ms（超时预算 5s→**0.5s**）；**计算 worker 必须独立进程**——线程受 GIL 争用使 500Hz 周期 p95 2.00→3.43ms、最坏 13.22ms，进程方案与基线无异且 IK 吞吐高 2.6×。结论已回写 FINAL_PLAN §1.4 与 §V8
- [x] 🔒 **M0-1 非阻塞循环 + 可抢占 EStop** ✅ J1 +2° / 200Hz / 1.5s 触发；estop 标志到 `set_stop()` 返回 **7.73ms**，周期 p50/p95=5.00/5.00ms、max=6.74ms；停止后速度接近 0
- [x] 🔒 **M0-2 自建 moveL 执行循环** ✅ 原定 MIT 路径经真机否决：2s 收敛后仍只移动 0.91mm、目标误差 9.53mm。改用 `Joint_Pos_Vel(iswait=False)` 逐点下发 + 末点保位：1cm 上移停止前实测 8.31mm、误差 1.73mm；`fraction` 严格单调；50.1% 取消正确进入 CANCELLED，停止前位移 3.46mm。`set_stop()` 后因重力回落 3.81–18.91mm，故轨迹精度一律在停止前验收，停止后只记漂移不要求回原位
- [x] **M0 收口** ✅ 控制周期锁定 **200Hz**；moveL 正式执行原语由 MIT 改为 POS-VEL；M0 三项全过，v1 业务 RPC 解锁

---

## 阶段 1：契约与仓库骨架

- [x] 🧪 **`proto/arm.proto`** ✅ 40 个 rpc（7 个流式）。已焊入核实修正：`MotorState` 删 `online`、补 `fault`/`mode`/`motor_time`/`pos_limit_flag`/`tor_limit_flag`/`valid`；IK 默认值经核实无需改并新增 `timeout_s`（默认 0.5s）；`avoid_collisions` 标 `deprecated` 并注明恒不生效；新增 `SetZero.persisted`、`SoftLimits.hardware_limits_enabled`、`DaemonStatus.estop_latch_hazard_present` 三个由核实结论倒逼出的字段
- [x] 🧪 **codegen** ✅ `proto/gen.sh` 生成 Python stub 到 `proto/gen/python/panthera_arm/`（含 `.pyi`，并修正 grpc 生成物的顶层 import 为包内相对 import）。C# 侧不重复生成物：由 Grpc.Tools 在 `dotnet build` 时按 csproj 引用同一份 `arm.proto` 生成
- [x] 🧪 **仓库骨架** ✅ `proto/ armd/ cli/ wpf/ deploy/` 就位；根 `pyproject.toml` 为 uv workspace（成员 armd / cli / proto/gen/python），`panthera-arm-proto` 以 workspace 依赖被两端共享，已 `uv` 解析构建通过
- [x] 🧪 **`armd --sim` 仿真后端** ✅ 6 关节 + 1 夹爪一阶电机模型；支持 POS-VEL / VELOCITY / MIT 整帧同模式下发、软限位、999.0 未连接哨兵、fault 注入、EStop 冻结、全体/逐电机归零持久化语义；`armd --sim --check` 可独立自检
- [x] 🧪 **HardwareLoop 骨架** ✅ 后端对象在线程内创建并独占；固定周期绝对时间基调度；每周期按 estop → cancel → 状态刷新 → 有界命令队列 → 非阻塞 motion step 顺序推进；N7 由 `JointFrame` 完整 7 槽同模式校验强制；EStop latch 与提交后立即 cancel 的竞态已覆盖
- [x] 🧪 **测试与 CI** ✅ 14 项 pytest 全走 `SimBackend`，覆盖整帧三模式/限位/归零/断连/线程独占/cancel/EStop<100ms/状态缓存；根 `make check` 一键执行 ruff + pytest + `armd --sim --check`；GitHub Actions 已接入

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
