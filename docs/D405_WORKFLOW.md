# RealSense D405 统一后端工作流

## 架构边界

```text
Windows
  └─ WPF 可视化终端 ── WSL TCP bridge ──┐
                                               │ gRPC :50051
WSL2 / Ubuntu                                  ▼
  panthera-cli ───────────────────────────────── armd
                                                 ├─ ArmService → HardwareLoop → Panthera-HT SDK → 机械臂
                                                 └─ CameraService → CameraWorker → librealsense RSUSB → D405
```

机械臂与 D405 都由 WSL2 中的同一个 `armd` 进程独占。ArmService 与
CameraService 共享端口、进程生命周期和部署服务。WPF 和 CLI 都是纯 gRPC
客户端；WPF 只做环境引导、状态/视频可视化和控制意图下发，不直接打开
RealSense SDK，也不存在独立 `camerad` 或 `:50052` 端口。

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

WPF 环境引导会按 VID/PID 查找并 attach 机械臂与 D405。也可在管理员
PowerShell 中手动执行：

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

`~/.config/panthera-wam/armd.env` 默认启用：

```dotenv
PANTHERA_CAMERA_MODE=auto
PANTHERA_CAMERA_WIDTH=640
PANTHERA_CAMERA_HEIGHT=480
PANTHERA_CAMERA_FPS=30
```

启动唯一后端：

```bash
systemctl --user restart armd
systemctl --user status armd --no-pager
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

所有命令都使用 `PANTHERA_ENDPOINT` 的同一个 `armd` 端点，不再使用
`PANTHERA_CAMERA_ENDPOINT`。深度帧为 Z16 PGM，像素值乘 JSON 中的
`depth_scale` 得到米；彩色帧为 RGB8 PPM。

## 仿真开发

```bash
uv run --package panthera-armd armd --sim --camera-mode sim --check
uv run --package panthera-armd armd --sim --camera-mode sim
uv run panthera camera status --json
```

仿真时机械臂和 D405 仍由同一 `armd` 托管，但不访问 USB。

## 2026-07-19 真机验收

- D405：`Intel RealSense D405`，SDK 序列号 `260422273428`，固件
  `5.13.0.55`，USB 3.2。
- vendored librealsense v2.58.1 以 RSUSB/libusb 后端源码构建，实际加载文件为
  `build/realsense-rsusb/Release/pyrealsense2...so`。
- D405 与机械臂 USB 同时 attach 到 Ubuntu-22.04；`640x480@30` depth Z16 +
  color RGB8 双流在普通 detach/attach 冷重连后连续 300 帧通过，0 次超时。
- 验证表明源码 RSUSB 后端不需要在每次 `armd` 启动时主动硬复位 D405。

## 故障定位

- WSL 找不到 D405：检查 `usbipd list` 是否为 `Attached`，以及
  `lsusb -d 8086:0b5b`。
- `pyrealsense2` 缺失或加载了 PyPI wheel：重跑
  `./deploy/build-realsense-wsl.sh`，然后检查 `python -c "import pyrealsense2 as rs; print(rs.__file__)"`。
- 权限错误：重新安装 `vendor/librealsense/config/99-realsense-libusb.rules`，再 reload
  udev 规则和重新 attach。
- 帧超时：确认 Windows RealSense Viewer 等程序已关闭，然后执行一次
  `usbipd detach` / `usbipd attach`；保持默认 `640x480@30`。
- CameraService 显示未启用：检查 `PANTHERA_CAMERA_MODE=auto` 并重启 `armd`。
