# Panthera-WAM

Panthera-HT 六轴机械臂的控制底座与 World Action Model 数据平台。

## 架构

```
Windows: WPF 控制终端 (Fluent, 三主题) ──┐
WSL:     panthera-cli (typer) ──────────┤→ gRPC (localhost:50051) → armd 守护服务 → 官方 Python SDK → usbipd → Panthera-HT
未来:    WAM 训练/推理 ─────────────────┘
```

- **armd**：WSL2 内常驻守护服务，独占硬件，封装官方 [Panthera-HT_SDK](https://github.com/HighTorque-Robotics/Panthera-HT_SDK)（零修改），提供控制权互斥 / watchdog / 软限位 / EStop 安全层
- **panthera-cli**：无损暴露 SDK 全部能力的命令行客户端
- **WPF 终端**：.NET 9 Fluent 主题（系统/浅色/深色），关节监控 + jog + 笛卡尔控制
- **v2**：拖动示教录制回放、RealSense D405 视频流、LeRobot 数据采集 → World Action Model

## 仓库规划

```
proto/    arm.proto 单一契约
armd/     守护服务 (Python)
cli/      panthera-cli (Python + typer)
wpf/      WPF 控制终端 (.NET 9)
deploy/   usbipd 脚本、systemd unit、安装文档
docs/     设计文档与计划
```

## 状态

计划已敲定（`docs/FINAL_PLAN.md`），WPF 视觉定稿为 C 稿驾驶舱（`docs/mockups/mockup-C-fluent-cockpit.html`）。阶段 1 的契约、仿真后端、HardwareLoop 骨架与 CI 已完成；M0-1/M0-2 仍等待真机在场验证，完成前不启动 v1 业务 RPC。最新进度见 `docs/MILESTONES.md`，开发约定见 `CLAUDE.md`。
