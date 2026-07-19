# RealSense D405 工作流

## 架构

```text
Windows USB 3 端口
  └─ usbipd bind/attach
       └─ WSL2: librealsense + pyrealsense2
            └─ armd CameraWorker（独立线程，latest-wins）
                 └─ CameraService（与 ArmService 共用 gRPC 端口）
                      ├─ panthera camera status/snapshot/stream
                      └─ WPF 视频面板与 LeRobot 采集（后续）
```

CameraWorker 不进入 200Hz HardwareLoop，也不持有机械臂 lease。相机断开、帧超时或 SDK
异常只会把 CameraService 标记为不可用，不会停止 armd 或影响机械臂安全闭环。

## 一次性安装

```bash
git submodule update --init --recursive
uv sync --all-packages --all-extras
./deploy/install-wsl.sh
sudo install -m 0644 vendor/librealsense/config/99-realsense-libusb.rules \
  /etc/udev/rules.d/99-realsense-libusb.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

`pyrealsense2` 固定为 `2.58.1.10581`，与 `vendor/librealsense` 的稳定版 `v2.58.1`
一致。

## Windows 挂载到 WSL

WPF 的环境引导会同时按 VID/PID 查找机械臂与 D405，并执行 bind/attach。D405 使用
`VID_8086&PID_0B5B`，不应长期保存易变化的 busid。

也可以在管理员 PowerShell 手动执行：

```powershell
usbipd list
usbipd bind --busid <D405_BUSID>
usbipd attach --wsl --busid <D405_BUSID>
```

重启 Windows 或重新插拔后若相机不在 WSL，通常只需再次执行 attach。

## 启动与检查

`~/.config/panthera-wam/armd.env` 应包含：

```dotenv
PANTHERA_CAMERA_MODE=auto
PANTHERA_CAMERA_SERIAL=
PANTHERA_CAMERA_WIDTH=640
PANTHERA_CAMERA_HEIGHT=480
PANTHERA_CAMERA_FPS=30
```

然后执行：

```bash
lsusb | rg -i '8086:0b5b|realsense'
systemctl --user restart armd
uv run panthera camera status --json
```

`available=true`、`streaming=true` 且 `last_frame_age_ms` 持续保持较小值，表示采集链路
正常。需要指定某台相机时填写 `PANTHERA_CAMERA_SERIAL`，不要使用 busid。

## 获取数据

保存一张 16-bit 深度图及 JSON 元数据：

```bash
uv run panthera camera snapshot --stream depth --out artifacts/d405-depth.pgm
```

保存彩色帧：

```bash
uv run panthera camera snapshot --stream color --out artifacts/d405-color.ppm
```

查看 30 帧元数据，或把帧序列写入目录：

```bash
uv run panthera camera stream --stream depth --frames 30 --rate-hz 10
uv run panthera camera stream --stream color --frames 30 --out-dir artifacts/d405-color
```

深度帧为 Z16 PGM，像素值乘 JSON 中的 `depth_scale` 得到米；彩色帧为 RGB8 PPM。
每个图像旁都有同名 `.json`，记录系统时间、设备时间、帧序号、步长与深度比例。

## 无设备开发

```bash
uv run --package panthera-armd armd --sim --camera-mode sim
uv run panthera camera status --json
uv run panthera camera snapshot --stream depth
```

仿真相机用于 CI、CLI 和 WPF 联调，不会访问 USB 设备。

## 故障定位

- Windows 能看到、WSL 看不到：检查 `usbipd list` 是否为 `Attached`，再执行 attach。
- WSL 能看到但权限不足：重新安装 `99-realsense-libusb.rules` 并重新 attach。
- `pyrealsense2` 缺失：执行 `uv sync --all-packages --all-extras`。
- 分辨率启动失败：先恢复 `640x480@30`；CameraService 会在后台周期性重连。
- gRPC 可用但没有帧：查看 `journalctl --user -u armd -f` 和 `camera status --json` 的
  `error` 字段。
