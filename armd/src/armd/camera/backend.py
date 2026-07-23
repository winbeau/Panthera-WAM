"""armd 内部的相机采集后端与 latest-wins 工作线程。"""

from __future__ import annotations

import os
import select
import shutil
import stat
import subprocess
import threading
import time
from base64 import b64decode
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Protocol


class CameraStream(str, Enum):
    DEPTH = "depth"
    COLOR = "color"


class CameraRole(str, Enum):
    WRIST = "wrist"
    OVERHEAD = "overhead"


class CameraPixelFormat(str, Enum):
    Z16 = "z16"
    RGB8 = "rgb8"
    JPEG = "jpeg"


class CameraUnavailableError(RuntimeError):
    """相机 SDK、设备或流配置当前不可用。"""


@dataclass(frozen=True, slots=True)
class CameraProfileInfo:
    stream: CameraStream
    pixel_format: CameraPixelFormat
    width: int
    height: int
    fps: int


@dataclass(frozen=True, slots=True)
class CameraDeviceInfo:
    model: str
    serial: str
    firmware: str
    usb_type: str
    sdk_version: str
    profiles: tuple[CameraProfileInfo, ...]
    role: CameraRole = CameraRole.WRIST


@dataclass(frozen=True, slots=True)
class RawCameraFrame:
    stream: CameraStream
    pixel_format: CameraPixelFormat
    device_timestamp_ms: float
    width: int
    height: int
    stride: int
    depth_scale: float
    data: bytes
    native_frame: object | None = None


@dataclass(frozen=True, slots=True)
class CameraFrameSnapshot:
    stream: CameraStream
    pixel_format: CameraPixelFormat
    sequence: int
    captured_at_ns: int
    device_timestamp_ms: float
    width: int
    height: int
    stride: int
    depth_scale: float
    data: bytes
    native_frame: object | None = None
    role: CameraRole = CameraRole.WRIST
    captured_monotonic_ns: int = 0


@dataclass(frozen=True, slots=True)
class CameraStatusSnapshot:
    enabled: bool
    available: bool
    streaming: bool
    model: str = ""
    serial: str = ""
    firmware: str = ""
    usb_type: str = ""
    sdk_version: str = ""
    error: str = ""
    last_frame_age_ms: int = -1
    actual_fps: float = 0.0
    profiles: tuple[CameraProfileInfo, ...] = ()
    role: CameraRole = CameraRole.WRIST


class CameraBackend(Protocol):
    def open(self) -> CameraDeviceInfo: ...

    def read(self) -> tuple[RawCameraFrame, ...]: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


class RealSenseCameraBackend:
    """独占一个 librealsense pipeline；只能由 CameraWorker 线程调用。"""

    def __init__(
        self,
        *,
        serial: str = "",
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        timeout_ms: int = 5000,
    ) -> None:
        self.serial = serial
        self.width = width
        self.height = height
        self.fps = fps
        self.timeout_ms = timeout_ms
        self._rs = None
        self._pipeline = None
        self._pipeline_lock = threading.Lock()
        self._interrupt = threading.Event()
        self._depth_scale = 0.0

    def open(self) -> CameraDeviceInfo:
        self._interrupt.clear()
        try:
            import pyrealsense2 as rs
        except ImportError as exc:
            raise CameraUnavailableError(
                "未安装 vendored pyrealsense2；请执行 ./deploy/build-realsense-wsl.sh"
            ) from exc

        context = rs.context()
        devices = list(context.query_devices())
        selected = None
        for device in devices:
            name = self._get_info(device, rs.camera_info.name)
            serial = self._get_info(device, rs.camera_info.serial_number)
            if self.serial and serial != self.serial:
                continue
            if "D405" in name or self.serial:
                selected = device
                break
        if selected is None:
            discovered = [self._get_info(device, rs.camera_info.name) for device in devices]
            detail = f"；已发现 {discovered}" if discovered else ""
            raise CameraUnavailableError(f"未找到 Intel RealSense D405{detail}")

        serial = self._get_info(selected, rs.camera_info.serial_number)
        pipeline = rs.pipeline(context)
        config = rs.config()
        config.enable_device(serial)
        config.enable_stream(rs.stream.depth, self.width, self.height, rs.format.z16, self.fps)
        config.enable_stream(rs.stream.color, self.width, self.height, rs.format.rgb8, self.fps)
        try:
            active_profile = pipeline.start(config)
        except Exception as exc:
            raise CameraUnavailableError(
                f"D405 无法启动 {self.width}x{self.height}@{self.fps} depth/color 流: {exc}"
            ) from exc

        active_device = active_profile.get_device()
        self._rs = rs
        with self._pipeline_lock:
            self._pipeline = pipeline
        self._depth_scale = float(active_device.first_depth_sensor().get_depth_scale())
        return CameraDeviceInfo(
            model=self._get_info(active_device, rs.camera_info.name),
            serial=self._get_info(active_device, rs.camera_info.serial_number),
            firmware=self._get_info(active_device, rs.camera_info.firmware_version),
            usb_type=self._get_info(active_device, rs.camera_info.usb_type_descriptor),
            sdk_version=self._sdk_version(rs),
            profiles=(
                CameraProfileInfo(
                    CameraStream.DEPTH,
                    CameraPixelFormat.Z16,
                    self.width,
                    self.height,
                    self.fps,
                ),
                CameraProfileInfo(
                    CameraStream.COLOR,
                    CameraPixelFormat.RGB8,
                    self.width,
                    self.height,
                    self.fps,
                ),
            ),
            role=CameraRole.WRIST,
        )

    def read(self) -> tuple[RawCameraFrame, ...]:
        with self._pipeline_lock:
            pipeline = self._pipeline
        if pipeline is None:
            raise CameraUnavailableError("D405 pipeline 尚未启动")
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        frames = None
        while not self._interrupt.is_set() and time.monotonic() < deadline:
            remaining_ms = max(1, round((deadline - time.monotonic()) * 1000))
            success, candidate = pipeline.try_wait_for_frames(min(100, remaining_ms))
            if success:
                frames = candidate
                break
        if self._interrupt.is_set():
            raise CameraUnavailableError("D405 采集已中断")
        if not frames:
            raise CameraUnavailableError(f"D405 在 {self.timeout_ms}ms 内未返回新帧")
        output: list[RawCameraFrame] = []
        depth = frames.get_depth_frame()
        if depth:
            output.append(self._copy_frame(depth, CameraStream.DEPTH, CameraPixelFormat.Z16))
        color = frames.get_color_frame()
        if color:
            output.append(self._copy_frame(color, CameraStream.COLOR, CameraPixelFormat.RGB8))
        if not output:
            raise CameraUnavailableError("D405 返回了空 frameset")
        return tuple(output)

    def interrupt(self) -> None:
        self._interrupt.set()

    def close(self) -> None:
        self._interrupt.set()
        with self._pipeline_lock:
            pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                pass

    def _copy_frame(
        self,
        frame,
        stream: CameraStream,
        pixel_format: CameraPixelFormat,
    ) -> RawCameraFrame:
        return RawCameraFrame(
            stream=stream,
            pixel_format=pixel_format,
            device_timestamp_ms=float(frame.get_timestamp()),
            width=int(frame.get_width()),
            height=int(frame.get_height()),
            stride=int(frame.get_stride_in_bytes()),
            depth_scale=self._depth_scale if stream is CameraStream.DEPTH else 0.0,
            data=b"",
            native_frame=frame,
        )

    @staticmethod
    def _get_info(device, key) -> str:
        try:
            return str(device.get_info(key)) if device.supports(key) else ""
        except Exception:
            return ""

    @staticmethod
    def _sdk_version(rs) -> str:
        sdk_version = getattr(rs, "__version__", "")
        if sdk_version:
            return str(sdk_version)
        try:
            native_module = import_module("pyrealsense2.pyrealsense2")
            sdk_version = getattr(native_module, "__version__", "")
            if sdk_version:
                return str(sdk_version)
        except ImportError:
            pass
        try:
            return version("pyrealsense2")
        except PackageNotFoundError:
            return "unknown"


class V4L2MjpegCameraBackend:
    """通过 v4l2-ctl mmap 流原样读取 C920e 的 MJPEG 帧。"""

    STABLE_DEVICE_PATH = "/home/winbeau/camera-devices/c920e"

    def __init__(
        self,
        *,
        device_path: str = STABLE_DEVICE_PATH,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        timeout_ms: int = 5000,
        max_frame_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.device_path = device_path
        self.width = width
        self.height = height
        self.fps = fps
        self.timeout_ms = timeout_ms
        self.max_frame_bytes = max_frame_bytes
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.Lock()
        self._interrupt = threading.Event()
        self._buffer = bytearray()

    def open(self) -> CameraDeviceInfo:
        self._interrupt.clear()
        self._buffer.clear()
        self._validate_configuration()
        executable = shutil.which("v4l2-ctl")
        if executable is None:
            raise CameraUnavailableError("缺少 v4l2-ctl；请安装 v4l-utils")

        device = Path(self.device_path)
        try:
            resolved = device.resolve(strict=True)
        except OSError as exc:
            raise CameraUnavailableError(f"C920e 稳定设备别名不可用: {self.device_path}") from exc
        try:
            if not stat.S_ISCHR(resolved.stat().st_mode):
                raise CameraUnavailableError(f"C920e 别名未指向字符设备: {resolved}")
        except OSError as exc:
            raise CameraUnavailableError(f"无法读取 C920e 设备状态: {resolved}") from exc

        probe = self._run_v4l2(
            executable,
            "--all",
            f"--set-fmt-video=width={self.width},height={self.height},pixelformat=MJPG",
            f"--set-parm={self.fps}",
            "--get-fmt-video",
            "--get-parm",
        )
        if "Video Capture" not in probe or "'MJPG'" not in probe:
            raise CameraUnavailableError("C920e 未确认到 MJPG Video Capture 能力")
        if f"Width/Height      : {self.width}/{self.height}" not in probe:
            raise CameraUnavailableError(f"C920e 未接受分辨率 {self.width}x{self.height}")
        if f"Frames per second: {self.fps:.3f}" not in probe:
            raise CameraUnavailableError(f"C920e 未接受 {self.fps} fps")

        process = subprocess.Popen(
            [
                executable,
                "--device",
                self.device_path,
                "--stream-mmap=4",
                "--stream-to=-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            process.kill()
            raise CameraUnavailableError("无法读取 v4l2-ctl 标准输出")
        with self._process_lock:
            self._process = process

        model = "Logitech C920e"
        for line in probe.splitlines():
            if "Card type" in line and ":" in line:
                model = line.split(":", 1)[1].strip() or model
                break
        sdk_version = self._run_v4l2(executable, "--version").splitlines()[0].strip()
        return CameraDeviceInfo(
            model=model,
            serial="",
            firmware="",
            usb_type="V4L2/UVC",
            sdk_version=sdk_version,
            profiles=(
                CameraProfileInfo(
                    CameraStream.COLOR,
                    CameraPixelFormat.JPEG,
                    self.width,
                    self.height,
                    self.fps,
                ),
            ),
            role=CameraRole.OVERHEAD,
        )

    def read(self) -> tuple[RawCameraFrame, ...]:
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        while not self._interrupt.is_set() and time.monotonic() < deadline:
            frame = self._extract_jpeg()
            if frame is not None:
                return (
                    RawCameraFrame(
                        stream=CameraStream.COLOR,
                        pixel_format=CameraPixelFormat.JPEG,
                        device_timestamp_ms=time.monotonic() * 1000.0,
                        width=self.width,
                        height=self.height,
                        stride=0,
                        depth_scale=0.0,
                        data=frame,
                    ),
                )

            with self._process_lock:
                process = self._process
            if process is None or process.stdout is None:
                raise CameraUnavailableError("C920e V4L2 流尚未启动")
            if process.poll() is not None:
                raise CameraUnavailableError(f"C920e v4l2-ctl 已退出，exit={process.returncode}")
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select((process.stdout,), (), (), min(0.25, remaining))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), 64 * 1024)
            if not chunk:
                raise CameraUnavailableError("C920e V4L2 流意外结束")
            self._buffer.extend(chunk)
            if len(self._buffer) > self.max_frame_bytes * 2:
                start = self._buffer.rfind(b"\xff\xd8")
                if start < 0:
                    self._buffer.clear()
                else:
                    del self._buffer[:start]
                if len(self._buffer) > self.max_frame_bytes:
                    raise CameraUnavailableError("C920e MJPEG 单帧超过大小上限")

        if self._interrupt.is_set():
            raise CameraUnavailableError("C920e 采集已中断")
        raise CameraUnavailableError(f"C920e 在 {self.timeout_ms}ms 内未返回完整 JPEG 帧")

    def interrupt(self) -> None:
        self._interrupt.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def close(self) -> None:
        self._interrupt.set()
        with self._process_lock:
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)

    def _extract_jpeg(self) -> bytes | None:
        start = self._buffer.find(b"\xff\xd8")
        if start < 0:
            return None
        if start > 0:
            del self._buffer[:start]
        end = self._buffer.find(b"\xff\xd9", 2)
        if end < 0:
            if len(self._buffer) > self.max_frame_bytes:
                raise CameraUnavailableError("C920e MJPEG 单帧超过大小上限")
            return None
        end += 2
        frame = bytes(self._buffer[:end])
        del self._buffer[:end]
        return frame

    def _validate_configuration(self) -> None:
        normalized = str(PurePosixPath(self.device_path))
        if normalized != self.STABLE_DEVICE_PATH:
            raise CameraUnavailableError(f"C920e 生产配置只允许稳定别名 {self.STABLE_DEVICE_PATH}")
        if os.name != "posix":
            raise CameraUnavailableError("C920e V4L2 后端只能运行在 Linux")
        if self.width <= 0 or self.height <= 0 or self.fps <= 0:
            raise CameraUnavailableError("C920e width/height/fps 必须为正整数")
        if not 1024 <= self.max_frame_bytes <= 16 * 1024 * 1024:
            raise CameraUnavailableError("C920e max_frame_bytes 必须在 1KiB–16MiB 之间")

    def _run_v4l2(self, executable: str, *arguments: str) -> str:
        result = subprocess.run(
            [executable, "--device", self.device_path, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(2.0, self.timeout_ms / 1000.0),
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
            raise CameraUnavailableError(f"v4l2-ctl 配置 C920e 失败: {detail}")
        return result.stdout


class SimCameraBackend:
    """CI 使用的确定性 D405 仿真帧源。"""

    def __init__(self, *, width: int = 64, height: int = 48, fps: int = 30) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self._next_frame_at = 0.0
        self._frame_index = 0

    def open(self) -> CameraDeviceInfo:
        self._next_frame_at = time.monotonic()
        return CameraDeviceInfo(
            model="RealSense D405 Simulator",
            serial="SIM-D405-0001",
            firmware="sim",
            usb_type="sim",
            sdk_version="sim",
            profiles=(
                CameraProfileInfo(
                    CameraStream.DEPTH,
                    CameraPixelFormat.Z16,
                    self.width,
                    self.height,
                    self.fps,
                ),
                CameraProfileInfo(
                    CameraStream.COLOR,
                    CameraPixelFormat.RGB8,
                    self.width,
                    self.height,
                    self.fps,
                ),
            ),
            role=CameraRole.WRIST,
        )

    def read(self) -> tuple[RawCameraFrame, ...]:
        delay = self._next_frame_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next_frame_at = max(self._next_frame_at + 1.0 / self.fps, time.monotonic())
        self._frame_index += 1
        depth_value = 1000 + self._frame_index % 100
        depth_pixel = depth_value.to_bytes(2, "little")
        depth = depth_pixel * (self.width * self.height)
        color_pixel = bytes(
            (self._frame_index % 256, (self._frame_index * 3) % 256, (self._frame_index * 7) % 256)
        )
        color = color_pixel * (self.width * self.height)
        timestamp_ms = time.monotonic() * 1000.0
        return (
            RawCameraFrame(
                CameraStream.DEPTH,
                CameraPixelFormat.Z16,
                timestamp_ms,
                self.width,
                self.height,
                self.width * 2,
                0.001,
                depth,
            ),
            RawCameraFrame(
                CameraStream.COLOR,
                CameraPixelFormat.RGB8,
                timestamp_ms,
                self.width,
                self.height,
                self.width * 3,
                0.0,
                color,
            ),
        )

    def close(self) -> None:
        pass

    def interrupt(self) -> None:
        pass


class SimOverheadCameraBackend:
    """CI/WPF 使用的确定性 C920e JPEG 仿真帧源。"""

    _JPEG_8X6 = b64decode(
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcU"
        "FhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgo"
        "KCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAAGAAgDASIAAhEB"
        "AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9"
        "AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
        "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ip"
        "qrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEB"
        "AQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJB"
        "UQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RV"
        "VldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6"
        "wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDjKKKK/Tj4"
        "g//Z"
    )

    def __init__(self, *, fps: int = 30) -> None:
        self.width = 8
        self.height = 6
        self.fps = fps
        self._next_frame_at = 0.0
        self._frame_index = 0
        self._interrupt = threading.Event()

    def open(self) -> CameraDeviceInfo:
        self._interrupt.clear()
        self._next_frame_at = time.monotonic()
        return CameraDeviceInfo(
            model="Logitech C920e Simulator",
            serial="SIM-C920E-0001",
            firmware="sim",
            usb_type="sim",
            sdk_version="sim",
            profiles=(
                CameraProfileInfo(
                    CameraStream.COLOR,
                    CameraPixelFormat.JPEG,
                    self.width,
                    self.height,
                    self.fps,
                ),
            ),
            role=CameraRole.OVERHEAD,
        )

    def read(self) -> tuple[RawCameraFrame, ...]:
        delay = self._next_frame_at - time.monotonic()
        if delay > 0 and self._interrupt.wait(delay):
            raise CameraUnavailableError("C920e 仿真采集已中断")
        if self._interrupt.is_set():
            raise CameraUnavailableError("C920e 仿真采集已中断")
        self._next_frame_at = max(self._next_frame_at + 1.0 / self.fps, time.monotonic())
        self._frame_index += 1
        return (
            RawCameraFrame(
                stream=CameraStream.COLOR,
                pixel_format=CameraPixelFormat.JPEG,
                device_timestamp_ms=time.monotonic() * 1000.0,
                width=self.width,
                height=self.height,
                stride=0,
                depth_scale=0.0,
                data=self._JPEG_8X6,
            ),
        )

    def close(self) -> None:
        self._interrupt.set()

    def interrupt(self) -> None:
        self._interrupt.set()


class CameraWorker:
    """在专用线程中独占后端，并只保留每种流的最新一帧。"""

    def __init__(
        self,
        backend_factory: Callable[[], CameraBackend],
        *,
        role: CameraRole = CameraRole.WRIST,
        reconnect_delay_s: float = 1.0,
    ) -> None:
        self._backend_factory = backend_factory
        self.role = role
        self._reconnect_delay_s = reconnect_delay_s
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_backend: CameraBackend | None = None
        self._frames: dict[CameraStream, CameraFrameSnapshot] = {}
        self._sequences = {CameraStream.DEPTH: 0, CameraStream.COLOR: 0}
        self._frame_times: deque[float] = deque(maxlen=60)
        self._last_frame_at = 0.0
        self._status = CameraStatusSnapshot(
            enabled=True,
            available=False,
            streaming=False,
            error=f"{role.value} 相机正在启动",
            role=role,
        )

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"panthera-camera-{self.role.value}",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            backend = self._active_backend
            self._condition.notify_all()
        if backend is not None:
            try:
                backend.interrupt()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        with self._condition:
            self._status = CameraStatusSnapshot(
                enabled=True,
                available=False,
                streaming=False,
                error="相机采集已停止",
                role=self.role,
            )

    def status(self) -> CameraStatusSnapshot:
        with self._condition:
            status = self._status
            age_ms = -1
            if self._last_frame_at > 0:
                age_ms = max(0, round((time.monotonic() - self._last_frame_at) * 1000))
            return CameraStatusSnapshot(
                enabled=status.enabled,
                available=status.available,
                streaming=status.streaming,
                model=status.model,
                serial=status.serial,
                firmware=status.firmware,
                usb_type=status.usb_type,
                sdk_version=status.sdk_version,
                error=status.error,
                last_frame_age_ms=age_ms,
                actual_fps=self._actual_fps_locked(),
                profiles=status.profiles,
                role=status.role,
            )

    def wait_for_frame(
        self,
        stream: CameraStream,
        *,
        after_sequence: int = 0,
        timeout_s: float = 2.0,
    ) -> CameraFrameSnapshot | None:
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while not self._stop.is_set():
                frame = self._frames.get(stream)
                if frame is not None and frame.sequence > after_sequence:
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            backend = self._backend_factory()
            with self._condition:
                self._active_backend = backend
            try:
                info = backend.open()
                if info.role is not self.role:
                    raise CameraUnavailableError(
                        f"相机角色不匹配：服务={self.role.value}，设备={info.role.value}"
                    )
                with self._condition:
                    self._status = CameraStatusSnapshot(
                        enabled=True,
                        available=True,
                        streaming=False,
                        model=info.model,
                        serial=info.serial,
                        firmware=info.firmware,
                        usb_type=info.usb_type,
                        sdk_version=info.sdk_version,
                        profiles=info.profiles,
                        role=info.role,
                    )
                    self._condition.notify_all()
                while not self._stop.is_set():
                    self._publish(backend.read())
            except Exception as exc:
                with self._condition:
                    self._status = CameraStatusSnapshot(
                        enabled=True,
                        available=False,
                        streaming=False,
                        error=str(exc),
                        role=self.role,
                    )
                    self._condition.notify_all()
            finally:
                backend.close()
                with self._condition:
                    if self._active_backend is backend:
                        self._active_backend = None
            self._stop.wait(self._reconnect_delay_s)

    def _publish(self, frames: tuple[RawCameraFrame, ...]) -> None:
        captured_at_ns = time.time_ns()
        captured_monotonic_ns = time.monotonic_ns()
        now = captured_monotonic_ns / 1_000_000_000
        with self._condition:
            for frame in frames:
                self._sequences[frame.stream] += 1
                self._frames[frame.stream] = CameraFrameSnapshot(
                    stream=frame.stream,
                    pixel_format=frame.pixel_format,
                    sequence=self._sequences[frame.stream],
                    captured_at_ns=captured_at_ns,
                    device_timestamp_ms=frame.device_timestamp_ms,
                    width=frame.width,
                    height=frame.height,
                    stride=frame.stride,
                    depth_scale=frame.depth_scale,
                    data=frame.data,
                    native_frame=frame.native_frame,
                    role=self.role,
                    captured_monotonic_ns=captured_monotonic_ns,
                )
            self._last_frame_at = now
            self._frame_times.append(now)
            status = self._status
            if status.available and not status.streaming:
                self._status = CameraStatusSnapshot(
                    enabled=status.enabled,
                    available=True,
                    streaming=True,
                    model=status.model,
                    serial=status.serial,
                    firmware=status.firmware,
                    usb_type=status.usb_type,
                    sdk_version=status.sdk_version,
                    profiles=status.profiles,
                    role=status.role,
                )
            self._condition.notify_all()

    def _actual_fps_locked(self) -> float:
        if len(self._frame_times) < 2:
            return 0.0
        elapsed = self._frame_times[-1] - self._frame_times[0]
        return 0.0 if elapsed <= 0 else (len(self._frame_times) - 1) / elapsed
