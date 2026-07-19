"""armd 内部的 RealSense D405 采集后端与 latest-wins 工作线程。"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol


class CameraStream(str, Enum):
    DEPTH = "depth"
    COLOR = "color"


class CameraPixelFormat(str, Enum):
    Z16 = "z16"
    RGB8 = "rgb8"


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
            data=bytes(frame.get_data()),
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


class CameraWorker:
    """在专用线程中独占后端，并只保留每种流的最新一帧。"""

    def __init__(
        self,
        backend_factory: Callable[[], CameraBackend],
        *,
        reconnect_delay_s: float = 1.0,
    ) -> None:
        self._backend_factory = backend_factory
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
            error="D405 正在启动",
        )

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="panthera-d405", daemon=True)
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
                with self._condition:
                    self._status = CameraStatusSnapshot(
                        enabled=True,
                        available=True,
                        streaming=True,
                        model=info.model,
                        serial=info.serial,
                        firmware=info.firmware,
                        usb_type=info.usb_type,
                        sdk_version=info.sdk_version,
                        profiles=info.profiles,
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
        now = time.monotonic()
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
                )
            self._last_frame_at = now
            self._frame_times.append(now)
            self._condition.notify_all()

    def _actual_fps_locked(self) -> float:
        if len(self._frame_times) < 2:
            return 0.0
        elapsed = self._frame_times[-1] - self._frame_times[0]
        return 0.0 if elapsed <= 0 else (len(self._frame_times) - 1) / elapsed
