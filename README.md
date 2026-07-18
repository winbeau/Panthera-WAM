# Panthera-WAM

Panthera-HT 六轴机械臂的控制底座与 World Action Model 数据平台。

## 当前状态

项目处于 **v1 收尾验收**：armd、27 条 CLI 命令和 WPF 控制闭环均已实现。
自动化覆盖仿真后端、真实后端 fake、lease/watchdog/EStop、状态与标定、
关节/夹爪、运动学、MoveL 执行状态以及 WPF 核心逻辑。

正式发布前还需要操作员在场完成三项真机签字：夹爪限位拒绝、MoveL 完成/取消、
全体非持久化归零。具体顺序与恢复步骤见
[`docs/V1_ACCEPTANCE.md`](docs/V1_ACCEPTANCE.md)。唯一进度来源仍是
[`docs/MILESTONES.md`](docs/MILESTONES.md)。

## 架构

```text
Windows: WPF 控制终端 ───────────────┐
WSL:     panthera-cli ───────────────┤→ gRPC → armd → 官方 SDK → usbipd → Panthera-HT
未来:    WAM 训练/推理与数据工具 ────┘
```

- **armd**：200Hz 单硬件线程守护服务，提供 lease、watchdog、软限位和 EStop 安全层。
- **panthera-cli**：27 条 v1 命令，纯 gRPC 客户端，不直接访问硬件。
- **WPF 终端**：.NET 9 Fluent 驾驶舱，系统/浅色/深色主题，关节、夹爪和 MoveL 控制。
- **v2**：阻抗与动力学、多点轨迹、拖动示教、D405、LeRobot/WAM 数据能力。

## 仿真开发

```bash
git submodule update --init --recursive
uv sync --all-packages --all-extras
make check
```

单独启动仿真服务：

```bash
uv run --package panthera-armd armd --sim
uv run panthera daemon status
uv run panthera --help
```

`make check` 执行 Ruff、全部 Python 测试和 200Hz 仿真自检，不接触真机。

## WSL 真机部署

```bash
./deploy/install-wsl.sh
systemctl --user start armd
systemctl --user status armd --no-pager
```

安装脚本默认不会启动服务。环境变量、udev 规则及日志命令见
[`deploy/README.md`](deploy/README.md)。

## WPF 构建与测试

在原生 Windows 终端运行：

```bat
wpf\tools\run-tests.cmd
```

该脚本执行 Release 构建、单元测试和 FlaUI 三主题界面测试，并把截图写入
`%USERPROFILE%\Desktop\Panthera-Design\ui-artifacts`。WPF build 与非 UI 单元测试也已纳入 GitHub Actions。

## 安全红线

- 未经操作员当次明确确认，禁止向真机发送 jog、MoveJ、MoveL、夹爪或归零命令。
- 日常开发和 CI 一律使用 `armd --sim`。
- EStop 不需要 lease；固件 watchdog 默认 150ms。
- `calibrate zero` 虽不产生运动，但会重定义坐标零点，必须按验收文档在最后执行并完成恢复。

详细架构决策见 [`docs/FINAL_PLAN.md`](docs/FINAL_PLAN.md)。
