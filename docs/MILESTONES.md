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
| 阶段 2 | v1：armd + CLI（M1–M4） | 4 / 4 ✅ |
| 阶段 3 | WPF v1（M-W0 – M-W3） | 5 / 5 ✅ |
| 阶段 4 | v2（M5–M9） | 5 / 5（实现）✅；真机运动验收待用户在场 |

---

## 阶段 -1：前置准备 ✅

- [x] 切出实施分支 `feat/foundation-m0`
- [x] 官方 SDK public fork + vendor ✅ `winbeau/Panthera-HT_SDK` 为公开 fork（上游 `HighTorque-Robotics/Panthera-HT_SDK`），主仓库以 `vendor/Panthera-HT_SDK` git submodule 固定版本；wsl-host 旧 `~/Panthera-HT_SDK` 副本仍可兼容
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

- [x] 🧪 **`proto/arm.proto`** ✅ 44 个 rpc（7 个流式）。已焊入核实修正：`MotorState` 删 `online`、补 `fault`/`mode`/`motor_time`/`pos_limit_flag`/`tor_limit_flag`/`valid`；IK 默认值经核实无需改并新增 `timeout_s`（默认 0.5s）；`avoid_collisions` 标 `deprecated` 并注明恒不生效；新增 `SetZero.persisted`、`SoftLimits.hardware_limits_enabled`、`DaemonStatus.estop_latch_hazard_present` 三个由核实结论倒逼出的字段；另增 4 个 WSL mirrored 兼容短请求 `HeartbeatOnce/GetRobotState/JointJogStep/StopJointJog`
- [x] 🧪 **codegen** ✅ `proto/gen.sh` 生成 Python stub 到 `proto/gen/python/panthera_arm/`（含 `.pyi`，并修正 grpc 生成物的顶层 import 为包内相对 import）。C# 侧不重复生成物：由 Grpc.Tools 在 `dotnet build` 时按 csproj 引用同一份 `arm.proto` 生成
- [x] 🧪 **仓库骨架** ✅ `proto/ armd/ cli/ wpf/ deploy/` 就位；根 `pyproject.toml` 为 uv workspace（成员 armd / cli / proto/gen/python），`panthera-arm-proto` 以 workspace 依赖被两端共享，已 `uv` 解析构建通过
- [x] 🧪 **`armd --sim` 仿真后端** ✅ 6 关节 + 1 夹爪一阶电机模型；支持 POS-VEL / VELOCITY / MIT 整帧同模式下发、软限位、999.0 未连接哨兵、fault 注入、EStop 冻结、全体/逐电机归零持久化语义；`armd --sim --check` 可独立自检
- [x] 🧪 **HardwareLoop 骨架** ✅ 后端对象在线程内创建并独占；固定周期绝对时间基调度；每周期按 estop → cancel → 状态刷新 → 有界命令队列 → 非阻塞 motion step 顺序推进；N7 由 `JointFrame` 完整 7 槽同模式校验强制；EStop latch 与提交后立即 cancel 的竞态已覆盖
- [x] 🧪 **测试与 CI** ✅ 57 项 pytest 默认不碰真机，覆盖 Sim/Real fake 后端整帧三模式、限位、归零、断连重连、线程独占、lease/cancel/EStop/watchdog/gRPC/CLI/运动学/笛卡尔执行；根 `make check` 一键执行 ruff + pytest + `armd --sim --check`；GitHub Actions 已接入 Python、Windows WPF Release/单测、FlaUI 四主题启动与截图产物

---

## 阶段 2：v1 —— armd + panthera-cli（27 条命令）

### M1 安全骨架
- [x] 🧪 实现 ✅ `armd --sim` 已监听 gRPC；实现 `control acquire/release/status/heartbeat`、metadata lease 统一拦截器、force-acquire 旧 token 失效、`estop trigger/reset`、`safety limits show`、daemon status/version、watchdog 后台任务；CLI 同步落地并持久化 lease token
- [x] 🧪 验收① ✅ 两客户端并发 acquire，第二个被拒并返回持有者 `client_id`；force-acquire 后旧 token 立即失效
- [x] 🧪 验收② ✅ 无 lease / 错 token 调 `JointMove` 均返回 `PERMISSION_DENIED`，有效 token 可穿过拦截器到业务层
- [x] 🧪 验收③ ✅ watchdog 超时自动 release；活动运动先安全取消，再统一进入零刚度软件柔顺阻尼，避免断线后永久刚性锁定；速度先经低通滤波再转连续阻尼力矩，避免无控制权拖动卡顿；迟到的 release 不能绕过 watchdog 停止
- [x] 🔒 验收④ ✅ gRPC 层 EStop 无需 lease；触发后运动 RPC 返回 `FAILED_PRECONDITION`，`reset --confirm` + 有效 lease 后恢复；真机抢占延迟由 M0-1 实测 7.73ms
- [x] 🧪 **N4 防护** ✅ `RealBackend` 每个状态周期及每次写帧前重新 `get_motors()`，不跨周期保存 SDK 悬垂指针；重连窗口返回 7 个无效快照并拒绝控制，fake 断开→重建句柄测试通过
- [x] 🧪 **N1 回归检查** ✅ 启动前扫描 SDK 自有 C++ 源码，`detect_motor_limit()` 除声明/定义外出现任何引用即拒绝启动；当前 SDK 记录为 Python 1.0.0 / C++ binding 4.4.7 / 电机 4.7.3，源码仍为零调用点
- [x] **N2 决策** ✅ 用户确认启用 **150ms** 固件看门狗；`RealBackend` 仅初始化期调用 `set_timeout(150)`，部署默认写入 `deploy/armd.env.example`
- [x] 🧪 **N5 固件门槛** ✅ 电机最低固件 <4.2.0 时拒绝状态查询和控制，避免“读状态”退化为 `velocity(0)` 写操作
- [x] 🔒 **RealBackend 真机验收** ✅ 7/7 电机在线，CANboard v4.8.6，电机均为 v4.7.3（`fun_v=5`）；gRPC `hardware_connected=true`、N1 hazard=false；200Hz 目标下含逐周期状态刷新的实测频率约 **191Hz**。同时修复控制频率统计误把约 3s SDK 初始化计入分母的问题

> M1 已收口：lease/EStop/watchdog、RealBackend、N1/N4/N5 防护与 150ms 固件看门狗均已落地；真机运行仍遵守逐次确认红线。

### M2 状态与标定
- [x] 🧪 实现 ✅ `state get`、`state watch`（`StreamState`）、`calibrate zero` 已接入 gRPC 与 CLI；归零强制 lease + `confirm=true` + 电机静止检查
- [x] 🧪 验收 ✅ `state watch` 可配置 0–100Hz 持续输出，断流可重新订阅；`age_ms` 来自 HardwareLoop 单调时钟缓存年龄
- [x] 🧪 **999.0 哨兵处理** ✅ 对外 `valid=false`，位置/速度/力矩清零，不把 SDK 的 999.0 当真实位置输出
- [x] 🔒 验收 ✅ 操作员现场确认全体归零没有产生物理运动；归零后 7 轴进入零参考，lease 已释放。停止服务并给控制器断电重启后，7/7 电机重新上线且恢复独立编码坐标，`fault=0`、模式 21，确认全体归零仅本次上电有效

### M3 关节 / 夹爪控制
- [x] 🧪 实现 ✅ `joint jog/move/movej`、`gripper open/close/move` 均已接入；位置运动使用 HardwareLoop 非阻塞状态机，CLI `--wait` 自动维持 heartbeat
- [x] 🧪 验收 ✅ 越限 `joint move` 被拒且包含关节名、方向和限位值；速度/力矩也在入队前检查
- [x] 🧪 验收 ✅ `joint jog` 关流立即归零，250ms 无新指令自动归零；近软限位 0.02rad 时屏蔽朝外速度并返回 `limit_hit`
- [x] 🔒 验收 ✅ J1 小幅 `movej --wait` 往返完成，前向误差 0.004798rad、回程误差 0.004414rad；等待期间 heartbeat 正常，取消/看门狗路径由仿真测试覆盖
- [x] 🔒 **桌面点动断线回归** ✅ J1 以 `0.02rad/s` 正向 5s、反向 5s（单程约 5.7°），控制链路全程持锁且 watchdog 正常；回位误差约 0.00063rad（0.036°），结束后 7/7 电机恢复模式 21 柔顺阻尼
- [x] 🔒 验收 ✅ `open --pos 2.01 --vel 0.0` 与 `close --pos -0.01 --vel 0.0` 均在入队前以退出码 2 拒绝，分别报告上限 2 与下限 0；操作员确认夹爪未动，拒绝前后 7 轴均无故障

### M4 笛卡尔与运动学
- [x] 🧪 实现 ✅ `kinematics fk/ik/jacobian/manipulability`、`cartesian movel/plan-preview`、`safety check-reached` 已接入 gRPC 与 CLI；计算仍隔离在独立 worker 进程
- [x] 🧪 验收 ✅ `plan-preview` 对不可达路径返回 `fraction<1` 且不执行运动；CLI 不暴露无效的 avoid-collisions 选项
- [x] 🧪 验收 ✅ `ik` 不可达返回 `found=false`，超时结构化返回 `timeout=true`，worker 异常不穿透服务进程
- [x] **安全收尾算法定案** ✅ `CancelExecution` 用 12 个控制周期按比例减速，watchdog 取消完成后切入零刚度软件柔顺阻尼；速度/加速度超限在轨迹入队前拒绝
- [x] 🔒 验收 ✅ `+Z 0.3cm / 2s` 完整执行进入 `EXEC_STATE_DONE`；`+Z 3cm / 4s` 的执行流 `fraction` 单调，在 50.623% 请求取消并进入 `EXEC_STATE_CANCELLED`。取消后重力回落约 1.14cm，仅记录、不判轨迹失败；`FAILED` 由仿真断连注入覆盖
- [x] **v1 完成口径** ✅ 27 条命令可用，M1–M4 自动化与真机场景全部通过

---

## 阶段 3：WPF v1（.NET 9 Fluent，视觉基准＝稿 C 驾驶舱）

- [x] **M-W0 脚手架** ✅ .NET 9 解决方案分层、Generic Host/DI、`ThemeMode` 三态、`IArmdClient` 与 Grpc.Tools 单契约生成均已落地；Windows Release 构建 0 警告
- [x] **M-W0.5 环境引导** ✅ usbipd 按 VID/PID/序列号发现设备，支持 WSL 启动、USB attach、串口数量与 armd 探活；修复 `wsl -l` Unicode/NUL 解析导致的误报
- [x] **M-W1 只读监控** ✅ 6 关节卡片、速度/力矩纵向 bar、俯视/侧视/主视三视图与 30fps latest-slot 渲染；Windows 侧改用 `GetRobotState` 短请求轮询以规避 mirrored 长流断线
- [x] **M-W2 控制闭环** ✅ 获取/释放控制权、关节点动、MoveJ、MoveL、夹爪、EStop 与取消均已接线；点动异常被后台监控并安全清理，不再从 `AsyncRelayCommand` 冒泡杀死进程；`%LOCALAPPDATA%/Panthera/terminal-failures.log` 持久记录未处理异常
- [x] **M-W2.5 WSL 控制桥** ✅ 桌面端常驻 `127.0.0.1:50050`，通过 `wsl.exe + nc` 标准流桥接 WSL 内 `50051`，完全绕开 WSL 2.5.7 mirrored 的不稳定 localhost 转发；15s 零速度控制压力测试与 J1 5s+5s 往返真机测试均通过
- [x] **M-W3 主题与键盘验收** ✅ System/Light/Dark/HighContrast 四主题均由 Windows FlaUI 实际启动并截图；隔离 UI 验收客户端不连接真机，自动获取虚拟控制权后逐项验证释放、主题、复位、EStop、MoveJ、MoveL、取消、夹爪和 12 个 Jog 按钮均可通过 Tab 到达，且焦点能完整循环。jog 松键、失焦、禁用自动停止与 F12/Esc 安全快捷键均已接入

---

## 阶段 4：v2

- [x] **M5 阻抗/动力学** ✅ `joint mit`/`gripper mit`、`cartesian jog` 与 6 项 `dynamics *` 已落地；流式新鲜度、软限位、默认 Fc/Fv 和仿真覆盖通过。🔒 真机 MIT/笛卡尔 jog 仅待用户在场逐次验收
- [x] **M6 多点轨迹** ✅ `trajectory run-waypoints` 复现两种 septic 插值分支，支持 execution 进度与 12 周期取消减速
- [x] **M7 拖动示教录制回放** ✅ 自由拖动补偿、非阻塞 JSONL writer、MIT/POS-VEL 回放、列表和取消已通过仿真。🔒 徒手阻力与真机回放误差待用户在场验收
- [x] **M8 相机流 + LeRobot 导出** ✅ `camera stream`、WPF 视频与 `dataset export-lerobot`
  - [x] D405 已在 Windows 识别为 `Intel(R) RealSense(TM) Depth Camera 405 Depth`（USB PID `0x0B5B`）；官方 `realsenseai/librealsense` 已 fork 到 `winbeau/librealsense`，并以 submodule 固定稳定版 `v2.58.1`
  - [x] 独立 `camera.proto`、WSL `camerad:50052`、gRPC 状态/快照/帧流，以及 `camera status/snapshot/stream` CLI 已接入；与 `armd:50051` 同属统一 Linux 后端
  - [x] WSL 默认/PyPI 采集路径曾出现 5s 帧超时；改用 `vendor/librealsense` v2.58.1 源码构建 `FORCE_RSUSB_BACKEND=ON` 后，libusb 持续双流通过
  - [x] WPF 环境引导按 `VID_8086&PID_0B5B` 发现 D405，并与机械臂一起 attach 到 WSL；WPF 分别通过 `armd:50051` 与 `camerad:50052` 查看状态
  - [x] WSL 统一后端真机验收：D405 序列号 `260422273428`、固件 `5.13.0.55`、USB 3.2；普通 detach/attach 冷重连后 `640x480@30` depth Z16 + color RGB8 连续 300 帧，0 次超时
  - [x] WPF 使用独立 `CameraEndpoint` 与第二条 WSL TCP bridge，显示 RGB8/Z16 双画面；相机不再误走机械臂端口
  - [x] 独立 `dataset.proto`、异步作业、取消/观察、字段映射与官方 LeRobotDataset v3 隔离 worker 已落地
- [x] **M9 无损审计收尾** ✅ `tools/audit_sdk_contract.py` 对 42 项方法逐条验证 SDK 源码、RPC 实现、CLI 与内部覆盖；结果 35 direct + 6 internal + 1 lifecycle，0 遗漏、0 无理由
- [x] **WPF v2 A 版控制台增量** ✅ 双 Tab 信息架构、精确 CAD 三视图、6 轴与夹爪状态、Ctrl 加减缩放、D405 双流、LeRobot 导出均已接线；示教录制升级为一键获取控制权、启动拖动示教、Linux JSONL 录制、停止保存、自动刷新选中与 POS-VEL 回放的完整会话，后端在 watchdog/示教结束时自动关闭 writer；三视图复用 `Panthera-HT-TriView` 子模块资源，并由 GitHub Actions 负责 Windows 构建与 UI 验收
- [x] **WPF v2.1 紧凑 Fluent 润色** ✅ 删除控制页左侧重复六轴/TCP 栏，三视图成为主视觉；右侧 Jog 六行同时承载点动与按真实软限位计算的关节状态条，三视图右列显示俯视图与 D405 RGB 实时画面，TCP XYZ 移到 CAD 标题栏；MoveJ/MoveL 改为左右常驻的 6+6 输入区，各自具有时长、执行和取消。CAD 在宽屏使用侧视/主视加宽布局、窄屏自动切换 2×2，固定镜头取景与模型尺寸已收紧；加入 PerMonitorV2 DPI 声明和 2560×1600@150% 自适应窗口，UI 最小缩放提高到 90%。已通过 Windows Release 构建、10 项单元测试及五项真实 FlaUI 主题/键盘验收
- [x] **无控制权柔顺拖动修复** ✅ 先经真机确认 `torque=0 / kp=0 / kd=0` 被动模式拖动顺滑，定位顿挫来自固件 `KD` 对离散速度反馈的直接响应；正式空闲策略改为 40ms 低通速度 + 限幅 `τ=-D·v_filtered` 软件阻尼，固件 `kp/kd` 均为零，静止力矩收敛到零。运动末点保位、夹爪动作阻尼与急停恢复阻尼保持独立；释放控制权先等待活动运动安全取消，超时升级为 EStop。armd 79 项回归测试与 ruff 全通过；🔒 软件柔顺阻尼真机手感待用户现场确认

---

## 阶段 5：Raspberry Pi 5 控制主机迁移

- [x] **M-P0 架构与配置** ✅ armd/camerad 支持环境变量远程 bind；WPF `Remote` 模式按
  Pi IP 直连并跳过 WSL bridge/usbipd，旧 `WslBridge` 模式保持兼容。最新主线 Linux
  90 项回归、SDK 42 项审计、armd 仿真自检、Windows WPF 19 项单测/Release 构建及
  WPF UI 验收均通过
- [x] **M-P1 ARM64 部署** ✅ Pi 5 使用系统 CPython 3.12.3 与 `uv sync --frozen`，从
  主仓库 vendor 安装 cp312 ARM64 SDK wheel，并从 vendored librealsense 2.58.1 构建
  RSUSB Python 绑定；armd/camerad 两项仿真自检通过，systemd 服务 enabled 但未自动启动
- [ ] **M-P2 只读联通**：`/dev/ttyACM0..6`、D405 `260422273428`、USB 3.2 与
  `640x480@30` 深度/彩色流均确认；Windows→Pi `50051/50052` 可达，远程 gRPC 对 armd
  仿真和真实 camerad 探活成功。WPF 已切换 `Remote` 配置；armd 真机服务保持 inactive
- [ ] 🔒 **M-P3 真机切换验收**：用户在场后按“读状态 → Enable → ≤5° 单关节 jog →
  EStop 演练”顺序验收，再允许 WPF 真机控制

---

## v1 发布状态

M1–M4、M-W0–M-W3、真机尾项、Windows Release 与发布包均已完成。逐项命令、
实测证据和恢复结果见 `docs/V1_ACCEPTANCE.md`。
