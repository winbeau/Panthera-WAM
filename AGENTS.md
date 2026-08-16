# Panthera-WAM

Panthera-HT 六轴机械臂（高擎 HighTorque）的控制底座与 World Action Model 数据平台。
当前状态：**实施进行中。进度以 `docs/MILESTONES.md` 为准**（M0、阶段 1 与 M1 已完成；RealBackend 已含 N1/N4/N5 防护，固件看门狗定为 150ms）。

## 必读文档（按顺序）

1. `docs/FINAL_PLAN.md` — **唯一权威计划**。架构决策、42 个 SDK 方法覆盖映射、arm.proto 草案、CLI 命令树、里程碑 M0→v1→WPF v1→v2、14 项审计修订。与其它文档冲突时以它为准。文末「**SDK 源码核实结论**」是逐行核对官方源码得到的一手事实（含 4 项契约修正与 N1–N10 新发现），**与 SDK README 冲突时以该节为准**（README 多处过时）。
2. `docs/MILESTONES.md` — **进度看板**。每项打勾即 commit+push；🔒 标记＝需真机且用户在场，不可自动执行。
3. `docs/JOINT_CONTROL.md` — **关节控制手册**。真机 CLI 控制正确姿势、lease/心跳流程、低速 jog 固件堵转锁死等 bug 解决、C1 六关节符号实测表。**动真机前先读**。
4. `docs/COORDINATE_CONTRACT.md` — **坐标/符号契约**。C1/C2/C3 核对结论、解除 teach 门控清单、J6 事故复盘。
5. `deploy/teach-cal.sh` — **示教参数标定脚本**。6 关节 × (kp,kd,fc,fv,scale) 增量微调（`--J1 "0,0.1,0,0,0"`，0 不改），状态持久化 `~/.config/panthera-wam/teach-cal.json`，`--show/--reset`。**当前定稿配置见手册 §6**。
6. `docs/CLI_PLAN.md` / `docs/WPF_PLAN.md` — 两侧的展开细节。
7. `docs/CAMERA_DEVICES.md` — Pi 5 上 C920e/D405 的稳定设备别名、序列号与采集约束。
8. `docs/mockups/mockup-C-fluent-cockpit.html` — **WPF 已定稿的视觉基准**（驾驶舱式：中央 SVG 雷达俯视图 + 左右圆形关节仪表 + jog pod）。A/B 两稿仅作参考。
9. `docs/DEVICE_REPAIR.md` — Pi 5 设备拔插后的 `repair-devices.sh` 恢复顺序、udev/ttyACM 映射、只读验收和安全退出码。

## 已敲定的决策（不要重新讨论）

- 架构：Raspberry Pi 5 ARM64 独占 Panthera-HT、D405 与 C920e → `armd:50051` / `camerad:50052` → gRPC+protobuf（Pi IP 或 SSH 隧道）→ 客户端 = `panthera-cli`（typer）+ WPF 终端（.NET 9 Fluent，ThemeMode 三态主题）。WSL2 仅保留兼容回退。
- 设备恢复：`deploy/repair-devices.sh c920e|realsense|can|all` 以 stop/start service 为主，按目标刷新 udev 和 `ttyACM*`，只做实际帧/状态验收；真实执行必须带 `--yes`，不得把它当作运动或 H0 绕过工具。
- armd 执行模型：HardwareLoop 单线程独占 `Panthera` 对象，**非阻塞逐周期步进**——严禁调用 SDK 的 `iswait=True`/`moveL()`/回放等内部阻塞循环。moveL 真机验证后改用 `Joint_Pos_Vel(iswait=False)` 逐点下发 + 末点保位收敛；SDK/MIT 路径在当前固件上跟踪失败。EStop 可抢占（实测 7.73ms）。
- 安全层：AcquireControl 控制权 lease（gRPC metadata 统一拦截）、watchdog 按控制模式分级停止、jog 用指令新鲜度窗口兜底（关节 250ms）、软限位入队前预检、EStop 直通不需持锁。
- 里程碑顺序硬约束：**M0 三项架构 spike 全过才允许开工 v1**（见 FINAL_PLAN「阶段 0」）。
- 仓库布局：`proto/`（单一契约）、`armd/`、`cli/`、`wpf/`、`deploy/`、`docs/`。
- 真机运动：**move/movej（单次目标帧）在当前固件必锁死（0x0B，重启 armd 恢复），真机运动一律 `joint jog`（≥0.3 rad/s）**；teach 录制/回放走 MIT 流式帧不受影响（`deploy/teach-cal.sh` 标定参数拉起）。
- 示教标定配置（定稿，2026-08-12）：每关节 5 参数 (kp,kd,fc,fv,scale)——J1(0,0.4,0.05,0.02,0.85*) J2(0,0.55,0.15,0.06,0.85) J3(0,0.6,0.15,0.06,1.15) J4(0,0.4,0.15,0.03,1.0) J5(0,0.15,0.02,0.01,0.85*) J6(0,0.08,0.02,0.01,0.85*)；*转动轴 scale 无效。标定工具 `deploy/teach-cal.sh`（增量微调，状态在 Pi 的 `~/.config/panthera-wam/teach-cal.json`），详见 `docs/JOINT_CONTROL.md` §6。

## 硬件与主机环境

| 事实 | 值 |
|---|---|
| 机械臂 | Panthera-HT，USB 复合设备 `VID_CAF1:FFFF`（序列号 2024051701），7×虚拟串口 |
| Windows 侧 busid | 仅用于 WSL 兼容回退；当前主路径不经 usbipd，busid 不得写入长期配置 |
| 相机 | 俯视 Logitech C920e；腕部 Intel RealSense D405（USB/UVC `251323070051`，librealsense SDK `260422273428`） |
| Pi 5 相机别名 | `/home/winbeau/camera-devices/c920e` 与 `/home/winbeau/camera-devices/realsense-{depth,infrared,color}`；完整表见 `docs/CAMERA_DEVICES.md` |
| 当前 H0 状态 | C920e/D405 服务与流状态已恢复；机械臂此前 7 电机为 `mode=0x0B`，H0 仍阻塞，不能由 service `active` 推断硬件健康 |
| WSL2 主机 | win-wsl2 = `ssh -p 2222 winbeau@100.78.122.53`（Ubuntu 22.04 + systemd，Tailscale）。**不要在 WSL 里跑 .exe**（interop 挂死），需要 Windows 命令直连下面这台 |
| Windows 主机 | winbeau-win = `ssh genev@100.92.156.126`（usbipd / dotnet build / WPF 运行都在这） |
| Windows 桌面 | `/mnt/c/Users/genev/Desktop`（视觉稿在 `Desktop/Panthera-Design/`） |
| 官方 SDK | public fork `https://github.com/winbeau/Panthera-HT_SDK`，以 git submodule 固定在 `vendor/Panthera-HT_SDK`；上游为 `HighTorque-Robotics/Panthera-HT_SDK`。装 whl：`motor_whl/hightorque_robot-1.2.0-cp3XX-*-linux_x86_64.whl`；Python 库在 `panthera_python/scripts/Panthera_lib/` |

判断你跑在哪：`/home/winbeau/camera-devices` 存在 → 当前就在 Pi 5 硬件侧；
`/mnt/c` 存在 → 当前在 WSL2/Windows 开发侧，真机服务与相机操作仍走 Pi 5 SSH；
其它环境默认视为远程开发机，armd/CLI 真机操作走 SSH（有 tmux-ssh-remote skill
就用它保持持久会话）。

相机代码与服务配置禁止固定 `/dev/videoN`。C920e/V4L2 使用
`/home/winbeau/camera-devices/` 下的稳定别名；`pyrealsense2` 必须用
`config.enable_device("260422273428")` 使用 librealsense SDK 序列号固定当前 D405；
USB/UVC 稳定别名中的 `251323070051` 不得传给 `enable_device`。metadata 别名不得作为普通图像源。

## 安全红线（机械臂会动，会伤人）

1. **未经用户当次明确确认，禁止向真机发送任何运动命令**（jog/moveJ/moveL/回放/使能后的任何写操作）。每次真机运动测试前都要确认用户在场。
2. 一切开发默认走 `armd --sim` 仿真后端；真机只用于集成验收。
3. 首次真机联调顺序：读状态 → Enable → 单关节小角度（≤5°）jog → EStop 演练 → 才允许 moveJ/moveL。
4. 真机测试脚本必须先打印将要执行的动作并二次确认；力矩限制用保守默认值。
5. `calibrate zero`（`set_reset_zero`）语义未核实前（FINAL_PLAN 风险 §2），不得对真机调用。
6. 设备故障恢复必须先运行 `./deploy/repair-devices.sh <target> --dry-run`；真实 `--yes` 执行前确认无示教、录制、lease、运动客户端，E-stop 可触达。若验收仍为 `mode=0x0B`，立即停止，不得用运动命令尝试解锁。

## 开发约定

- proto 是单一契约源：改 `proto/arm.proto` 后必须同步重新生成 Python 与 C# stub，两端一起提交。
- SDK 是 `vendor/Panthera-HT_SDK` git submodule：主仓库只固定 gitlink，不直接修改或复制 SDK 源码；SDK 变更必须在 public fork 独立提交，再更新主仓库 gitlink（`_execute_trajectory` 等私有逻辑的等价重写除外，且必须与 SDK 单体调用对拍验证，见 FINAL_PLAN 风险 §5）。
- armd/CLI：Python 3.10+，类型标注，pytest 全走 `--sim`；WPF：.NET 9，CommunityToolkit.Mvvm，csproj 压制 `WPF0001`（ThemeMode 实验性 API，已知已接受）。
- 提交信息中文，前缀 `feat:/fix:/docs:/test:/chore:`，一个里程碑验收项一个 PR 粒度的 commit。
- FINAL_PLAN「风险与开放问题」里列的"实现前必须核实"事项（pinocchio 双实例化、`set_reset_zero` 语义、继承层签名、MotorState 字段），在触及对应模块前先核实并把结论回写进 FINAL_PLAN。
- **Pi 上 `git pull` 只更新磁盘文件，不会热加载正在运行的 armd/camerad 进程**（`uv run --package panthera-armd armd` 是启动时装载代码）。修复后真机症状不变时，第一步先比对 `systemctl --user show armd.service -p ActiveEnterTimestamp`（进程启动时间）与最后一次 pull 的时间戳；进程旧 → 先重启再复测，**禁止对旧进程反复回环找 bug**（实例：start-record 的 `state=drag` 连查多轮，根因就是 armd 未重启，新代码根本没在跑）。
- **armd 重启必须在臂回到初始低位之后**（`rezero` → `zero-home`）：高位定死锁/阻尼锁状态下重启会中断持续帧流，150ms 看门狗坠臂（已发生 2 次事故）；高位发现需要重启时先回低位。
