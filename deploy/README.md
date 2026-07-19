# 部署

## Windows WPF 快速发布

首次在 Windows PowerShell / PowerShell 7 中执行：

```powershell
.\deploy\setup-dotnet9.ps1
```

该脚本会在 PowerShell 7 与 Windows PowerShell 的当前用户 Profile 中写入受标记管理的
配置块，固定优先使用 `$HOME\.dotnet\dotnet.exe` 的 .NET 9 SDK，清理会导致 SDK 误选的
`MSBuildSDKsPath`，并注册 `panthera-wpf` 快捷命令。重新打开终端后可直接使用：

```powershell
# 最快：初始化 CAD 子模块并生成自包含 win-x64 程序
panthera-wpf

# 与 GitHub CI 同口径：Release 构建、单测、FlaUI 四主题验收、发布
panthera-wpf -Mode Ci

# 与 Windows Installer workflow 同口径：测试、发布、Inno 安装包、SHA256、安装/卸载烟测
panthera-wpf -Mode Installer
```

默认可执行文件位于：

```text
wpf\installer\publish\Panthera.Terminal.App.exe
```

安装包位于 `wpf\installer\output\Panthera-Terminal-v*-win-x64-setup.exe`。本地生成安装包前
需安装 Inno Setup 6：

```powershell
winget install JRSoftware.InnoSetup
```

GitHub 的 `CI` workflow 也会上传名为 `panthera-terminal-win-x64` 的自包含程序 artifact，
不再只构建而不保留可下载执行程序。

## WSL 部署

`armd` 与 `camerad` 以 systemd user service 运行。两者都在同一 Linux
后端内，分别提供 `:50051` 机械臂服务和 `:50052` D405 服务。WPF 只作为
这两个 gRPC 服务的可视化终端。

## 首次安装

```bash
git submodule update --init --recursive
uv python install 3.11
uv sync --all-packages --all-extras
sudo apt-get update
sudo apt-get install -y build-essential libssl-dev libusb-1.0-0-dev pkg-config
./deploy/build-realsense-wsl.sh
./deploy/install-wsl.sh
sudo install -m 0644 deploy/99-panthera-ht.rules /etc/udev/rules.d/
sudo install -m 0644 vendor/librealsense/config/99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

检查 `~/.config/panthera-wam/armd.env` 中的 SDK 与机器人配置路径，然后执行：

```bash
systemctl --user start camerad armd
systemctl --user status camerad armd --no-pager
uv run panthera daemon status
```

安装脚本默认不启动服务，避免在机械臂尚未完成 USB 挂载或现场检查时访问硬件。
如环境已经就绪，可使用 `./deploy/install-wsl.sh --start`。

## 日常操作

```bash
systemctl --user restart camerad armd
journalctl --user -u camerad -u armd -f
systemctl --user stop armd camerad
```

也可在 `~/.zshrc` 中加载仓库内的统一恢复命令：

```zsh
[[ -r "$HOME/Panthera-WAM-v2/deploy/panthera-up.zsh" ]] && \
    source "$HOME/Panthera-WAM-v2/deploy/panthera-up.zsh"
```

之后执行 `panthera-up`，会依次检查机械臂 USB、D405 USB、Python 3.11、
电机 SDK 与 vendored librealsense，然后在同一 WSL 内启动隔离的 `armd` 和
`camerad`。WPF 分别连接机械臂和相机端点，不直接访问硬件 SDK。

Windows 侧先用 WPF 一键引导，或以管理员 PowerShell 将机械臂与 D405
都执行 `usbipd attach --wsl --busid <BUSID>`。程序按 VID/PID 与序列号发现
设备，不应把当前 busid 写进长期配置。

D405 使用 vendored librealsense RSUSB/libusb 后端，由 WSL `camerad` 独占；
机械臂与相机服务共享 Linux 生命周期，但不共享 gRPC 端口。
安装、采集和故障定位流程见 [`docs/D405_WORKFLOW.md`](../docs/D405_WORKFLOW.md)。

## 安全约束

- 服务启动和状态读取不代表获准执行运动。
- 任何真机 jog、MoveJ、MoveL、夹爪或归零验收，都需要操作员当次在场确认。
- 固件 watchdog 默认是 150ms；除非重新完成安全评估，不要设为 0。
