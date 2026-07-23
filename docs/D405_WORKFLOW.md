# RealSense D405 统一后端工作流

> **当前 Pi 5 部署（2026-07-23）**：D405 已迁移到 Raspberry Pi 5，USB/UVC 序列号为
> `251323070051`，librealsense SDK 序列号为 `260422273428`。深度、红外和彩色 V4L2 节点分别使用
> `/home/winbeau/camera-devices/realsense-depth`、`realsense-infrared`、
> `realsense-color`，不得固定 `/dev/videoN`；完整别名和 C920e 约定见
> `docs/CAMERA_DEVICES.md`。下文 WSL/usbipd 内容保留为兼容回退与历史验收记录。

## 架构边界

```text
Windows
  └─ WPF 可视化终端 ── WSL TCP bridge ──┬─ gRPC :50051
                                         └─ gRPC :50052
WSL2 / Ubuntu
  panthera-cli ─────────────┬─────────────────── armd :50051
                            │                     └─ ArmService → HardwareLoop → Panthera-HT SDK → 机械臂
                            └─────────────────── camerad :50052
                                                  └─ CameraService → librealsense RSUSB → D405
```

机械臂与 D405 都由同一套 WSL2 后端控制，但使用两个隔离进程：`armd` 独占
机械臂并监听 `:50051`，`camerad` 独占 D405 并监听 `:50052`。两者由同一个
`panthera-up` 或 systemd user services 统一启动、停止和检查，但不强行共享进程
或端口。WPF 和 CLI 都是纯 gRPC 客户端；WPF 只做环境引导、状态/视频可视化
和控制意图下发，不直接打开任何硬件 SDK。

## 一次性 WSL 安装

```bash
git submodule update --init --recursive
uv python install 3.11
uv sync --all-packages --all-extras
sudo apt-get update
sudo apt-get install -y build-essential libssl-dev libusb-1.0-0-dev pkg-config
./deploy/build-realsense-wsl.sh
```

`build-realsense-wsl.sh` 从 `vendor/librealsense` 固定的 v2.58.1 源码构建 Python
绑定，并强制 `FORCE_RSUSB_BACKEND=ON`。该后端通过 libusb 直接访问 D405，
不依赖 WSL 默认内核缺失的 V4L2/UVC 设备节点。

安装机械臂和 RealSense udev 规则：

```bash
sudo install -m 0644 deploy/99-panthera-ht.rules /etc/udev/rules.d/
sudo install -m 0644 vendor/librealsense/config/99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
./deploy/install-wsl.sh
```

## 将两类硬件挂载到同一 WSL

WPF 环境引导会按 VID/PID 查找并 attach 机械臂与 D405。推荐在 Windows
PowerShell 安装动态连接监督任务：

```powershell
./deploy/attach-wsl-usb.ps1
./deploy/attach-wsl-usb.ps1 -InstallWatcher
```

监督任务每 2 秒读取 `usbipd state`，设备断电、拔插、换 USB 口或 WSL
重启后，会按新的 BUSID 自动恢复两台设备。运行日志位于
`%LOCALAPPDATA%\Panthera-WAM\usb-auto-attach.log`。首次 bind 如遇权限错误，
在管理员 PowerShell 运行第一条命令。也可手动执行：

```powershell
usbipd list
usbipd bind --busid <PANTHERA_BUSID>
usbipd bind --busid <D405_BUSID>
usbipd attach --wsl --busid <PANTHERA_BUSID>
usbipd attach --wsl --busid <D405_BUSID>
```

`busid` 可能随插拔变化，不得写入长期配置。D405 的 Windows 设备标识是
`VID_8086&PID_0B5B`；WSL 中应能看到 `lsusb -d 8086:0b5b`。

## 启动与验收

`~/.config/panthera-wam/armd.env` 可配置 D405：

```dotenv
PANTHERA_CAMERA_WIDTH=640
PANTHERA_CAMERA_HEIGHT=480
PANTHERA_CAMERA_FPS=30
```

启动统一 WSL 后端：

```bash
systemctl --user restart camerad armd
systemctl --user status camerad armd --no-pager
uv run panthera daemon status
uv run panthera camera status --json
```

保存深度和彩色快照：

```bash
uv run panthera camera snapshot --stream depth --out artifacts/d405-depth.pgm
uv run panthera camera snapshot --stream color --out artifacts/d405-color.ppm
```

持续流验收：

```bash
uv run panthera camera stream --stream depth --frames 300 --rate-hz 30
uv run panthera camera stream --stream color --frames 300 --rate-hz 30
```

客户端分别配置两个端点：

```bash
export PANTHERA_ENDPOINT='[::1]:50051'
export PANTHERA_CAMERA_ENDPOINT='[::1]:50052'
```

Windows WPF 使用对应的 `localhost:50051` 和 `localhost:50052`；当前主路径部署在
树莓派 5，Remote 模式改用 Linux 主机的内网地址，SshRemote 模式使用本地隧道端口。
深度帧为 Z16 PGM，像素值乘 JSON 中的
`depth_scale` 得到米；彩色帧为 RGB8 PPM。

树莓派 5 部署将两个 service 的 `--bind` 从 `127.0.0.1` 改为树莓派内网
地址或 `0.0.0.0`，客户端分别连接 `<pi-ip>:50051` 与 `<pi-ip>:50052`。机械臂
控制端口在加入内网监听前必须配防火墙/访问控制，不能直接暴露到公网。

## 仿真开发

```bash
uv run --package panthera-armd armd --sim --check
uv run --package panthera-armd armd --sim
uv run panthera camera status --json
```

仿真快捷模式仍可由单个 `armd` 托管机械臂和相机模拟器，不访问 USB；真机部署
固定使用 `armd:50051` 与 `camerad:50052` 双服务。

## 2026-07-19 真机验收

- D405：`Intel RealSense D405`，SDK 序列号 `260422273428`，固件
  `5.13.0.55`，USB 3.2。
- vendored librealsense v2.58.1 以 RSUSB/libusb 后端源码构建，实际加载文件为
  `build/realsense-rsusb/Release/pyrealsense2...so`。
- D405 与机械臂 USB 同时 attach 到 Ubuntu-22.04；`640x480@30` depth Z16 +
  color RGB8 双流在普通 detach/attach 冷重连后连续 300 帧通过，0 次超时。
- 验证表明源码 RSUSB 后端不需要在每次 `camerad` 启动时主动硬复位 D405。

## 2026-07-23 Pi 5 设备节点验收

- 当前 D405 USB/UVC 序列号为 `251323070051`；`pyrealsense2` 必须使用
  librealsense SDK 序列号 `config.enable_device("260422273428")` 固定同一设备。
- 稳定别名已覆盖 depth、infrared、color 及各自 metadata 节点，重启后不依赖
  `/dev/videoN` 编号。
- 深度 Z16、红外 GREY、彩色 YUYV 均已实际采集成功。
- 同机 C920e 已通过 1080p MJPEG 30 fps 采集，用作固定俯视 RGB 画面。

## 故障定位

- WSL 找不到 D405：检查 `usbipd list` 是否为 `Attached`，以及
  `lsusb -d 8086:0b5b`。
- Pi 5 上 V4L2 节点异常：先检查 `ls -l /home/winbeau/camera-devices/` 与
  `readlink -f`，不得用临时 `/dev/videoN` 覆盖稳定配置。
- `pyrealsense2` 缺失或加载了 PyPI wheel：重跑
  `./deploy/build-realsense-wsl.sh`，然后检查 `python -c "import pyrealsense2 as rs; print(rs.__file__)"`。
- 权限错误：重新安装 `vendor/librealsense/config/99-realsense-libusb.rules`，再 reload
  udev 规则和重新 attach。
- 帧超时：确认 Windows RealSense Viewer 等程序已关闭，然后执行一次
  `usbipd detach` / `usbipd attach`；保持默认 `640x480@30`。
- CameraService 显示 camerad 不可用：检查 `systemctl --user status camerad`、
  `ss -ltnp | grep 50052` 和 camerad 日志，再重启 `camerad armd`。
