# RealSense D405 工作流

## 架构

```text
Windows
  ├─ Intel RealSense D405 → pyrealsense2 → camerad → CameraService :50052
  └─ WPF 控制终端 ───────────────────────┬→ CameraService :50052
                                         └→ WSL bridge → ArmService :50051

WSL2
  ├─ panthera-cli ────────────────────────┬→ CameraService :50052
  │                                       └→ ArmService :50051
  └─ armd → Panthera-HT SDK → usbipd → 机械臂
```

D405 留在 Windows，机械臂继续由 WSL2 独占。`camerad` 与 `armd` 是两个独立进程、两个
gRPC 服务和两个故障域；相机异常不会进入 200Hz HardwareLoop，也不会影响 lease、watchdog
或 EStop。

### 为什么不把 D405 attach 到 WSL

实机验证中，D405 经 usbipd attach 后可以枚举、保存短快照，但持续流会间歇出现
`Frame didn't arrive within 5000`。WSL 内核日志同时反复报告 USB/IP 虚拟主控的
`vhci_get_frame_number()` 尚未实现。该回调直接影响视频设备的 USB 帧调度，因此正式采集
链路改为 Windows 原生；usbipd 只保留给机械臂控制板。

## Windows 一次性安装

在 Windows PowerShell 中克隆仓库并安装：

```powershell
git clone --recurse-submodules https://github.com/winbeau/Panthera-WAM.git
cd Panthera-WAM
powershell -ExecutionPolicy Bypass -File camera\tools\install-windows.ps1
```

安装脚本使用 Python 3.11，并安装与 `vendor/librealsense` v2.58.1 对齐的
`pyrealsense2==2.58.1.10581`。

如果 `usbipd list` 显示 D405 为 `Attached`，先归还 Windows：

```powershell
usbipd detach --busid <D405_BUSID>
```

WPF 环境引导也会按 `VID_8086&PID_0B5B` 发现 D405，并在它误挂到 WSL 时自动 detach。

## 启动 camerad

在 Windows PowerShell 中执行：

```powershell
powershell -ExecutionPolicy Bypass -File camera\tools\run-windows.ps1
```

默认监听 `127.0.0.1:50052`，采集 depth Z16 与 color RGB8，分辨率为
`640x480@30`。多相机环境可指定 SDK 序列号：

```powershell
camera\tools\run-windows.ps1 -Serial 260422273428
```

## CLI 检查与采集

Windows 与 mirrored-networking WSL 均使用独立相机端点：

```bash
export PANTHERA_CAMERA_ENDPOINT=127.0.0.1:50052
uv run panthera camera status --json
```

保存一张 16-bit 深度图及 JSON 元数据：

```bash
uv run panthera camera snapshot --stream depth --out artifacts/d405-depth.pgm
```

保存彩色帧：

```bash
uv run panthera camera snapshot --stream color --out artifacts/d405-color.ppm
```

检查持续帧流，或把帧序列写入目录：

```bash
uv run panthera camera stream --stream depth --frames 300 --rate-hz 30
uv run panthera camera stream --stream color --frames 30 --out-dir artifacts/d405-color
```

深度帧为 Z16 PGM，像素值乘 JSON 中的 `depth_scale` 得到米；彩色帧为 RGB8 PPM。
每个图像旁都有同名 `.json`，记录系统时间、设备时间、帧序号、步长与深度比例。

## 无设备开发

```bash
uv run --package panthera-camera camerad --mode sim --check
uv run --package panthera-camera camerad --mode sim
uv run panthera camera status --json
```

仿真相机用于 CI、CLI 和 WPF 联调，不访问 USB 设备。

## 故障定位

- Windows 找不到 D405：检查 USB 3 线缆、端口和设备管理器中的 `VID_8086&PID_0B5B`。
- `usbipd list` 显示 `Attached`：执行 `usbipd detach --busid <BUSID>`。
- `pyrealsense2` 缺失：重新运行 `camera\tools\install-windows.ps1`。
- camerad 已启动但 WSL 连接失败：确认 WSL 使用 mirrored networking，并检查 Windows
  防火墙是否允许本机端口 `50052`。
- 帧超时：关闭 RealSense Viewer 等占用相机的程序，再恢复默认 `640x480@30`。
