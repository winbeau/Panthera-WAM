# Panthera-WAM

[![CI](https://github.com/winbeau/Panthera-WAM/actions/workflows/ci.yml/badge.svg)](https://github.com/winbeau/Panthera-WAM/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/winbeau/Panthera-WAM)](https://github.com/winbeau/Panthera-WAM/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Panthera-HT 六轴机械臂的控制底座与 World Action Model 数据平台。

## 当前状态

**v1.0.0 已完成**：armd、27 条 CLI 命令、WPF 控制闭环和自包含 Windows Release
均已交付。自动化覆盖仿真后端、真实后端 fake、lease/watchdog/EStop、状态与标定、
关节/夹爪、运动学、MoveL 执行状态，以及 WPF 四主题和完整键盘焦点循环。

真机验收已完成夹爪限位拒绝、MoveL DONE/CANCELLED、全体非持久化归零及断电恢复。
证据见 [`docs/V1_ACCEPTANCE.md`](docs/V1_ACCEPTANCE.md)，唯一进度来源是
[`docs/MILESTONES.md`](docs/MILESTONES.md)。Windows 用户可从
[GitHub Releases](https://github.com/winbeau/Panthera-WAM/releases/latest) 下载单文件
`win-x64-setup.exe` 安装程序，无需预装 .NET SDK。

## 架构

```text
Windows: WPF 控制终端 ───────────────┬→ CameraService → camerad → RealSense D405
WSL:     panthera-cli ───────────────┼→ ArmService → armd → 官方 SDK → usbipd → Panthera-HT
未来:    WAM 训练/推理与数据工具 ────┘
```

- **armd**：200Hz 单硬件线程守护服务，提供 lease、watchdog、软限位和 EStop 安全层。
- **panthera-cli**：27 条 v1 命令，纯 gRPC 客户端，不直接访问硬件。
- **WPF 终端**：.NET 9 Fluent 驾驶舱，系统/浅色/深色主题，关节、夹爪和 MoveL 控制。
- **D405 v2 基础链路**：Windows 原生 camerad → gRPC 状态/快照/帧流 → CLI/WPF。
- **v2 后续**：阻抗与动力学、多点轨迹、拖动示教、WPF 视频、LeRobot/WAM 数据能力。

## 仿真开发

```bash
git submodule update --init --recursive
uv python install 3.11
uv sync --all-packages --all-extras
make check
```

单独启动仿真服务：

```bash
uv run --package panthera-armd armd --sim
uv run panthera daemon status
uv run panthera --help
uv run --package panthera-camera camerad --mode sim --check
```

`make check` 执行 Ruff、全部 Python 测试和 200Hz 仿真自检，不接触真机。
D405 的 Windows 原生采集命令见 [`docs/D405_WORKFLOW.md`](docs/D405_WORKFLOW.md)。

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

该脚本执行 Release 构建、单元测试，以及 FlaUI 的 System/Light/Dark/HighContrast
四主题测试，并验证获取/释放控制、主题、复位、EStop、MoveJ、MoveL、取消、夹爪和
12 个 Jog 按钮都能通过 Tab 到达且焦点可循环。截图写入
`%USERPROFILE%\Desktop\Panthera-Design\ui-artifacts`。同一门禁已纳入 GitHub Actions。

正式 Windows 安装程序由
[`windows-installer.yml`](.github/workflows/windows-installer.yml) 使用 Inno Setup 构建；
未来 `v*` 标签会自动生成并上传安装 EXE，也支持手动附加到已有 Release。

## 安全红线

- 未经操作员当次明确确认，禁止向真机发送 jog、MoveJ、MoveL、夹爪或归零命令。
- 日常开发和 CI 一律使用 `armd --sim`。
- EStop 不需要 lease；固件 watchdog 默认 150ms。
- `calibrate zero` 虽不产生运动，但会重定义坐标零点，必须按验收文档在最后执行并完成恢复。

详细架构决策见 [`docs/FINAL_PLAN.md`](docs/FINAL_PLAN.md)。

## License

本项目以 [MIT License](LICENSE) 开源。Panthera-HT SDK 与 Apache-2.0 的 RealSense SDK 2.0
均通过 `vendor/` 下的 git submodule 引用，其许可证与使用条件以对应 SDK 仓库为准。
