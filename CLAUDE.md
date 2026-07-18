# Panthera-WAM

Panthera-HT 六轴机械臂（高擎 HighTorque）的控制底座与 World Action Model 数据平台。
当前状态：**v1.0.0 已完成。进度以 `docs/MILESTONES.md` 为准**；RealBackend 已含 N1/N4/N5 防护，固件看门狗定为 150ms。

## 必读文档（按顺序）

1. `docs/FINAL_PLAN.md` — **唯一权威计划**。架构决策、42 个 SDK 方法覆盖映射、arm.proto 草案、CLI 命令树、里程碑 M0→v1→WPF v1→v2、14 项审计修订。与其它文档冲突时以它为准。文末「**SDK 源码核实结论**」是逐行核对官方源码得到的一手事实（含 4 项契约修正与 N1–N10 新发现），**与 SDK README 冲突时以该节为准**（README 多处过时）。
2. `docs/MILESTONES.md` — **进度看板**。每项打勾即 commit+push；🔒 标记＝需真机且用户在场，不可自动执行。
3. `docs/CLI_PLAN.md` / `docs/WPF_PLAN.md` — 两侧的展开细节。
3. `docs/mockups/mockup-C-fluent-cockpit.html` — **WPF 已定稿的视觉基准**（驾驶舱式：中央 SVG 雷达俯视图 + 左右圆形关节仪表 + jog pod）。A/B 两稿仅作参考。

## 已敲定的决策（不要重新讨论）

- 架构：WSL2 独占硬件（usbipd）→ `armd` 守护服务（Python，封装官方 SDK **零修改**）→ gRPC+protobuf（`localhost:50051`）→ 客户端 = `panthera-cli`（typer）+ WPF 终端（.NET 9 Fluent，ThemeMode 三态主题）。
- armd 执行模型：HardwareLoop 单线程独占 `Panthera` 对象，**非阻塞逐周期步进**——严禁调用 SDK 的 `iswait=True`/`moveL()`/回放等内部阻塞循环。moveL 真机验证后改用 `Joint_Pos_Vel(iswait=False)` 逐点下发 + 末点保位收敛；SDK/MIT 路径在当前固件上跟踪失败。EStop 可抢占（实测 7.73ms）。
- 安全层：AcquireControl 控制权 lease（gRPC metadata 统一拦截）、watchdog 按控制模式分级停止、jog 用指令新鲜度窗口兜底（关节 250ms）、软限位入队前预检、EStop 直通不需持锁。
- 里程碑顺序硬约束：**M0 三项架构 spike 全过才允许开工 v1**（见 FINAL_PLAN「阶段 0」）。
- 仓库布局：`proto/`（单一契约）、`armd/`、`cli/`、`wpf/`、`deploy/`、`docs/`。

## 硬件与主机环境

| 事实 | 值 |
|---|---|
| 机械臂 | Panthera-HT，USB 复合设备 `VID_CAF1:FFFF`，7×虚拟串口；具体序列号通过本地设置提供，不提交到仓库 |
| Windows 侧 busid | 由 `usbipd state --json` 动态发现；挂进 WSL：管理员 PowerShell `usbipd attach --wsl --busid <BUSID>` |
| 相机 | Intel RealSense D405（v2 才用，届时同样 usbipd 挂 WSL） |
| WSL2 主机 | Ubuntu 22.04 + systemd；远程地址与账号保存在操作者本地配置，不提交到仓库 |
| Windows 主机 | usbipd、.NET build 与 WPF 运行在原生 Windows；远程地址与账号不入库 |
| Windows 桌面 | `%USERPROFILE%\Desktop`（UI 测试产物默认位于 `Panthera-Design\ui-artifacts`） |
| 官方 SDK | public fork `https://github.com/winbeau/Panthera-HT_SDK`，以 git submodule 固定在 `vendor/Panthera-HT_SDK`；上游为 `HighTorque-Robotics/Panthera-HT_SDK`。装 whl：`motor_whl/hightorque_robot-1.2.0-cp3XX-*-linux_x86_64.whl`；Python 库在 `panthera_python/scripts/Panthera_lib/` |

判断你跑在哪：`/mnt/c` 存在 → 你就在 WSL2 本地；否则真机操作需使用操作者本地保存的 SSH 配置。任何远程地址、用户名与设备序列号都不得写入仓库。

## 安全红线（机械臂会动，会伤人）

1. **未经用户当次明确确认，禁止向真机发送任何运动命令**（jog/moveJ/moveL/回放/使能后的任何写操作）。每次真机运动测试前都要确认用户在场。
2. 一切开发默认走 `armd --sim` 仿真后端；真机只用于集成验收。
3. 首次真机联调顺序：读状态 → Enable → 单关节小角度（≤5°）jog → EStop 演练 → 才允许 moveJ/moveL。
4. 真机测试脚本必须先打印将要执行的动作并二次确认；力矩限制用保守默认值。
5. `calibrate zero` 已验证为全体、非持久化且不产生运动；仍必须放在所有运动验收之后，并通过断电恢复原坐标。

## 开发约定

- proto 是单一契约源：改 `proto/arm.proto` 后必须同步重新生成 Python 与 C# stub，两端一起提交。
- SDK 是 `vendor/Panthera-HT_SDK` git submodule：主仓库只固定 gitlink，不直接修改或复制 SDK 源码；SDK 变更必须在 public fork 独立提交，再更新主仓库 gitlink（`_execute_trajectory` 等私有逻辑的等价重写除外，且必须与 SDK 单体调用对拍验证，见 FINAL_PLAN 风险 §5）。
- armd/CLI：Python 3.10+，类型标注，pytest 全走 `--sim`；WPF：.NET 9，CommunityToolkit.Mvvm，csproj 压制 `WPF0001`（ThemeMode 实验性 API，已知已接受）。
- 提交信息中文，前缀 `feat:/fix:/docs:/test:/chore:`，一个里程碑验收项一个 PR 粒度的 commit。
- FINAL_PLAN「风险与开放问题」里列的"实现前必须核实"事项（pinocchio 双实例化、`set_reset_zero` 语义、继承层签名、MotorState 字段），在触及对应模块前先核实并把结论回写进 FINAL_PLAN。
