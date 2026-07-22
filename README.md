# Panthera-WAM

[![CI](https://github.com/winbeau/Panthera-WAM/actions/workflows/ci.yml/badge.svg)](https://github.com/winbeau/Panthera-WAM/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/winbeau/Panthera-WAM)](https://github.com/winbeau/Panthera-WAM/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Panthera-HT 六轴机械臂的控制底座与 World Action Model 数据平台。

## 当前状态

**v2 实现已完成**：armd/camerad、50 条 CLI 命令、WPF 双端口视频驾驶舱和
LeRobotDataset v3 导出均已接入。自动化覆盖仿真后端、真实后端 fake、
lease/watchdog/EStop、MIT/动力学、笛卡尔 jog、多点轨迹、示教录制回放、
D405 帧流、数据导出，以及 42 项 SDK 能力的机器可执行审计。

真机验收已完成夹爪限位拒绝、MoveL DONE/CANCELLED、全体非持久化归零及断电恢复。
证据见 [`docs/V1_ACCEPTANCE.md`](docs/V1_ACCEPTANCE.md)，唯一进度来源是
[`docs/MILESTONES.md`](docs/MILESTONES.md)。Windows 用户可从
[GitHub Releases](https://github.com/winbeau/Panthera-WAM/releases/latest) 下载单文件
`win-x64-setup.exe` 安装程序，无需预装 .NET SDK。

## 架构

```text
Windows: WPF 可视化终端 ── gRPC / Pi IP ──┬→ armd :50051 → Arm/DatasetService → 机械臂/数据
                                          └→ camerad :50052 → CameraService → D405
Pi 5:    panthera-cli ─────────────────────→ 同一 ARM64 Linux 后端
Legacy:  WSL bridge :50050/:50049 仍保留作兼容与开发回退
```

- **armd**：200Hz 单硬件线程守护服务，提供 lease、watchdog、软限位和 EStop 安全层。
- **panthera-cli**：50 条 v1/v2 命令，纯 gRPC 客户端，不直接访问硬件。
- **WPF 终端**：.NET 9 Fluent 驾驶舱，只通过 gRPC 做状态/视频可视化与控制意图下发。
- **D405 视频链路**：D405 与机械臂直接连接 Raspberry Pi 5；`camerad:50052`
  隔离 librealsense 采集，WPF 以 RGB/深度 latest-frame 双画面显示。
- **v2 控制与数据**：MIT/动力学、笛卡尔 jog、多点轨迹、拖动示教录制回放，
  以及独立 `dataset.proto` 的 LeRobotDataset v3 异步导出。
- **无损审计**：[`docs/sdk-capability-audit.json`](docs/sdk-capability-audit.json)
  逐项记录 42 个 SDK 方法；`make check` 自动验证 SDK 源码、RPC、CLI 和内部覆盖。

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
uv run --package panthera-armd armd --sim --check
```

`make check` 执行 Ruff、全部 Python 测试和 200Hz 仿真自检，不接触真机。
D405 与机械臂的统一 WSL/armd 工作流见
[`docs/D405_WORKFLOW.md`](docs/D405_WORKFLOW.md)。

## Raspberry Pi 5 控制主机部署

Pi 5 使用系统 ARM64 Python 3.12、`uv` 工作区、主仓库内的 SDK/librealsense
vendor submodule。安装脚本会启用 systemd user service，但只运行仿真自检，不会访问真机：

```bash
git submodule update --init --recursive
./deploy/install-pi5.sh --bind-address <PI_TAILSCALE_OR_LAN_IP>
```

WPF 设为 `BackendMode=Remote`，机械臂端点使用 `http://<PI_IP>:50051`，相机端点使用
`http://<PI_IP>:50052`。建议绑定 Tailscale IP；当前链路是明文 gRPC，不应直接暴露到公网。

## Legacy WSL 部署

```bash
./deploy/install-wsl.sh
systemctl --user start camerad armd
systemctl --user status camerad armd --no-pager
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
