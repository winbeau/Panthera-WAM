# Raspberry Pi 5 相机设备约定

> 当前硬件主机：Raspberry Pi 5。设备别名由 udev 按设备型号和 RealSense
> 序列号生成，不受 `/dev/video0`、`/dev/video2` 等动态编号变化影响，重启后仍然有效。

## 稳定设备别名

| 设备/节点 | 稳定路径 | 用途 |
|---|---|---|
| Logitech C920e 视频 | `/home/winbeau/camera-devices/c920e` | 固定俯视 RGB 画面 |
| Logitech C920e metadata | `/home/winbeau/camera-devices/c920e-metadata` | UVC metadata，不作为普通图像源 |
| RealSense D405 深度 | `/home/winbeau/camera-devices/realsense-depth` | Z16 深度流 |
| RealSense D405 深度 metadata | `/home/winbeau/camera-devices/realsense-depth-metadata` | 深度 metadata |
| RealSense D405 红外 | `/home/winbeau/camera-devices/realsense-infrared` | GREY 红外流 |
| RealSense D405 红外 metadata | `/home/winbeau/camera-devices/realsense-infrared-metadata` | 红外 metadata |
| RealSense D405 彩色 | `/home/winbeau/camera-devices/realsense-color` | YUYV 彩色流 |
| RealSense D405 彩色 metadata | `/home/winbeau/camera-devices/realsense-color-metadata` | 彩色 metadata |

当前 D405 序列号为 `251323070051`。

## 服务角色与端口

两台相机复用同一份 `CameraService` protobuf 契约，但由两个独立进程提供服务，
避免 C920e/V4L2 阻塞或崩溃影响 D405 深度流。

| 相机角色 | Pi 端服务 | Pi 端口 | SshRemote 本地端口 | 生产设备选择 |
|---|---|---:|---:|---|
| 腕部 `WRIST` | `camerad` | `50052` | `50049` | D405 序列号 `251323070051` |
| 俯视 `OVERHEAD` | `overhead-camera` | `50053` | `50048` | `/home/winbeau/camera-devices/c920e` |

客户端连接后必须校验服务上报的相机角色。角色与目标画面不一致时应明确报错，
不得悄悄回退到另一台相机。WPF 只消费预览流；后续 LingBot-VA 数据录制在 Pi
本地完成，并使用单调时钟时间戳与机械臂状态、腕部画面对齐。

## 代码与配置约束

1. 生产代码、systemd 配置和长期配置中禁止固定 `/dev/videoN`；V4L2/OpenCV
   必须使用上表稳定别名。
2. C920e 是俯视摄像头；D405 是腕部摄像头。不得因枚举顺序变化交换二者角色。
3. 使用 `pyrealsense2` 时按序列号选择 D405：

   ```python
   config.enable_device("251323070051")
   ```

4. D405 的同步深度/红外/彩色采集优先使用 `pyrealsense2`/vendored
   librealsense；V4L2 别名主要用于节点诊断和单流验收。
5. `*-metadata` 节点不是普通视频画面，OpenCV 不应把它们当作图像设备打开。
6. C920e 采集端保持 MJPEG，gRPC 传输 JPEG 编码帧；不得先展开为 1080p RGB8
   再通过网络传输。WPF 预览默认限制为 10–15 fps，录制端仍可保留 30 fps。

## OpenCV 示例

```python
import cv2

camera = cv2.VideoCapture("/home/winbeau/camera-devices/c920e")
```

C920e 已实际通过 `1920×1080`、MJPEG、30 fps 采集验收。需要明确请求该模式时：

```python
camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
camera.set(cv2.CAP_PROP_FPS, 30)
```

## 2026-07-23 真机验收

- C920e：1080p MJPEG 30 fps 采集成功。
- RealSense 深度节点：Z16 采集成功。
- RealSense 红外节点：GREY 采集成功。
- RealSense 彩色节点：YUYV 采集成功。
- D405 序列号：`251323070051`。
- 所有别名均指向 udev 生成的稳定路径，重启后不依赖 `/dev/videoN` 编号。

## 快速诊断

```bash
ls -l /home/winbeau/camera-devices/
readlink -f /home/winbeau/camera-devices/c920e
readlink -f /home/winbeau/camera-devices/realsense-depth
```

若别名缺失，先检查 udev 规则、设备 USB 连接和 D405 序列号；不要把临时出现的
`/dev/videoN` 写回配置作为修复。
