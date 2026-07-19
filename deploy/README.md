# WSL 部署

`armd` 以 systemd user service 运行，与 WPF 环境引导中的
`systemctl --user start armd` 保持一致。

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
systemctl --user start armd
systemctl --user status armd --no-pager
uv run panthera daemon status
```

安装脚本默认不启动服务，避免在机械臂尚未完成 USB 挂载或现场检查时访问硬件。
如环境已经就绪，可使用 `./deploy/install-wsl.sh --start`。

## 日常操作

```bash
systemctl --user restart armd
journalctl --user -u armd -f
systemctl --user stop armd
```

Windows 侧先用 WPF 一键引导，或以管理员 PowerShell 将机械臂与 D405
都执行 `usbipd attach --wsl --busid <BUSID>`。程序按 VID/PID 与序列号发现
设备，不应把当前 busid 写进长期配置。

D405 使用 vendored librealsense RSUSB/libusb 后端，由同一 `armd` 进程托管；
安装、采集和故障定位流程见 [`docs/D405_WORKFLOW.md`](../docs/D405_WORKFLOW.md)。

## 安全约束

- 服务启动和状态读取不代表获准执行运动。
- 任何真机 jog、MoveJ、MoveL、夹爪或归零验收，都需要操作员当次在场确认。
- 固件 watchdog 默认是 150ms；除非重新完成安全评估，不要设为 0。
