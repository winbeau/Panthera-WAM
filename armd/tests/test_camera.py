from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import threading
import time

import grpc
import pytest
from panthera_arm import camera_pb2, camera_pb2_grpc

from armd.backend import SimBackend
from armd.camera.backend import (
    CameraDeviceInfo,
    CameraPixelFormat,
    CameraProfileInfo,
    CameraRole,
    CameraStream,
    CameraUnavailableError,
    CameraWorker,
    SimCameraBackend,
    SimOverheadCameraBackend,
    V4L2MjpegCameraBackend,
)
from armd.camera.service import CameraService
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer


def test_camera_worker_keeps_latest_depth_and_color_frames() -> None:
    worker = CameraWorker(lambda: SimCameraBackend(width=8, height=6, fps=60))
    worker.start()
    try:
        depth = worker.wait_for_frame(CameraStream.DEPTH, timeout_s=1.0)
        color = worker.wait_for_frame(CameraStream.COLOR, timeout_s=1.0)
        status = worker.status()
    finally:
        worker.stop()

    assert depth is not None
    assert color is not None
    assert depth.width == color.width == 8
    assert depth.height == color.height == 6
    assert len(depth.data) == 8 * 6 * 2
    assert len(color.data) == 8 * 6 * 3
    assert status.available
    assert status.streaming
    assert status.model == "RealSense D405 Simulator"
    assert status.role is CameraRole.WRIST
    assert depth.captured_monotonic_ns > 0


def test_overhead_camera_worker_keeps_latest_jpeg_frame() -> None:
    worker = CameraWorker(
        lambda: SimOverheadCameraBackend(fps=60),
        role=CameraRole.OVERHEAD,
    )
    worker.start()
    try:
        color = worker.wait_for_frame(CameraStream.COLOR, timeout_s=1.0)
        status = worker.status()
    finally:
        worker.stop()

    assert color is not None
    assert color.role is CameraRole.OVERHEAD
    assert color.pixel_format is CameraPixelFormat.JPEG
    assert color.data.startswith(b"\xff\xd8")
    assert color.data.endswith(b"\xff\xd9")
    assert color.captured_monotonic_ns > 0
    assert status.available
    assert status.streaming
    assert status.role is CameraRole.OVERHEAD
    assert status.model == "Logitech C920e Simulator"


def test_v4l2_mjpeg_parser_discards_prefix_and_preserves_next_frame() -> None:
    backend = V4L2MjpegCameraBackend()
    first = b"\xff\xd8first\xff\xd9"
    second = b"\xff\xd8second\xff\xd9"
    backend._buffer.extend(b"transport-noise" + first + second)

    assert backend._extract_jpeg() == first
    assert backend._extract_jpeg() == second
    assert backend._extract_jpeg() is None


def test_v4l2_rejects_dynamic_video_node() -> None:
    backend = V4L2MjpegCameraBackend(device_path="/dev/video0")
    with pytest.raises(CameraUnavailableError, match="稳定别名"):
        backend._validate_configuration()


def test_camera_worker_stop_interrupts_blocking_backend() -> None:
    class BlockingBackend:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def open(self) -> CameraDeviceInfo:
            return CameraDeviceInfo(
                model="Blocking D405",
                serial="BLOCKING",
                firmware="test",
                usb_type="test",
                sdk_version="test",
                profiles=(
                    CameraProfileInfo(
                        CameraStream.DEPTH,
                        CameraPixelFormat.Z16,
                        8,
                        6,
                        30,
                    ),
                ),
            )

        def read(self):
            self.closed.wait(30)
            raise RuntimeError("closed")

        def interrupt(self) -> None:
            self.closed.set()

        def close(self) -> None:
            self.closed.set()

    backend = BlockingBackend()
    worker = CameraWorker(lambda: backend)
    worker.start()
    deadline = time.monotonic() + 1.0
    while not worker.status().available and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.status().available

    started = time.monotonic()
    worker.stop()

    assert backend.closed.is_set()
    assert time.monotonic() - started < 1.0


def test_camerad_sim_check() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "armd.camera",
            "--mode",
            "sim",
            "--width",
            "8",
            "--height",
            "6",
            "--local-bind",
            "[::1]:0",
            "--check",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(result.stdout)
    monotonic_ns = payload.pop("captured_monotonic_ns")
    assert monotonic_ns > 0
    assert payload == {
        "available": True,
        "streaming": True,
        "model": "RealSense D405 Simulator",
        "role": "CAMERA_DEVICE_ROLE_WRIST",
        "pixel_format": "CAMERA_PIXEL_FORMAT_Z16",
        "width": 8,
        "height": 6,
        "bytes": 96,
    }


def test_camerad_overhead_sim_check() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "armd.camera",
            "--mode",
            "sim",
            "--role",
            "overhead",
            "--fps",
            "60",
            "--check",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(result.stdout)
    assert payload["available"] is True
    assert payload["streaming"] is True
    assert payload["model"] == "Logitech C920e Simulator"
    assert payload["role"] == "CAMERA_DEVICE_ROLE_OVERHEAD"
    assert payload["pixel_format"] == "CAMERA_PIXEL_FORMAT_JPEG"
    assert payload["bytes"] > 0
    assert payload["captured_monotonic_ns"] > 0


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM graceful shutdown is a POSIX deployment contract")
def test_camerad_sigterm_stops_cleanly() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "armd.camera",
            "--mode",
            "sim",
            "--bind",
            "127.0.0.1:0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 15.0)
        assert ready, "camerad 未在 15 秒内完成启动"
        assert "camerad 已启动" in process.stdout.readline()
        process.terminate()
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.asyncio
async def test_camera_grpc_status_snapshot_and_finite_stream() -> None:
    hardware_loop = HardwareLoop(SimBackend, control_hz=200.0)
    camera_worker = CameraWorker(lambda: SimCameraBackend(width=8, height=6, fps=60))
    server = ArmdServer(
        hardware_loop,
        bind="127.0.0.1:0",
        camera_worker=camera_worker,
    )
    hardware_loop.start()
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = camera_pb2_grpc.CameraServiceStub(channel)
    try:
        for _ in range(20):
            status = await stub.GetStatus(camera_pb2.CameraStatusRequest())
            if status.available and status.streaming:
                break
            await asyncio.sleep(0.01)
        assert status.available
        assert status.streaming
        assert len(status.profiles) == 2
        assert status.role == camera_pb2.CAMERA_DEVICE_ROLE_WRIST

        depth = await stub.CaptureFrame(
            camera_pb2.CaptureFrameRequest(
                stream=camera_pb2.CAMERA_STREAM_TYPE_DEPTH,
                timeout_ms=1000,
            )
        )
        assert depth.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_Z16
        assert len(depth.data) == 8 * 6 * 2
        assert depth.role == camera_pb2.CAMERA_DEVICE_ROLE_WRIST
        assert depth.captured_monotonic_ns > 0

        frames = []
        async for frame in stub.StreamFrames(
            camera_pb2.StreamFramesRequest(
                stream=camera_pb2.CAMERA_STREAM_TYPE_COLOR,
                max_rate_hz=60,
                max_frames=3,
            )
        ):
            frames.append(frame)
        assert len(frames) == 3
        assert [frame.sequence for frame in frames] == sorted(frame.sequence for frame in frames)
        assert all(frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_RGB8 for frame in frames)
    finally:
        await channel.close()
        await server.stop()
        hardware_loop.stop()


@pytest.mark.asyncio
async def test_armd_proxies_internal_camerad() -> None:
    camera_worker = CameraWorker(lambda: SimCameraBackend(width=8, height=6, fps=60))
    camera_server = grpc.aio.server()
    camera_pb2_grpc.add_CameraServiceServicer_to_server(CameraService(camera_worker), camera_server)
    camera_port = camera_server.add_insecure_port("127.0.0.1:0")
    camera_worker.start()
    await camera_server.start()

    hardware_loop = HardwareLoop(SimBackend, control_hz=200.0)
    server = ArmdServer(
        hardware_loop,
        bind="127.0.0.1:0",
        camera_endpoint=f"127.0.0.1:{camera_port}",
        additional_binds=("127.0.0.1:0",),
    )
    hardware_loop.start()
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.additional_ports[0]}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = camera_pb2_grpc.CameraServiceStub(channel)
    try:
        for _ in range(20):
            status = await stub.GetStatus(camera_pb2.CameraStatusRequest())
            if status.available and status.streaming:
                break
            await asyncio.sleep(0.01)
        assert status.model == "RealSense D405 Simulator"
        depth = await stub.CaptureFrame(
            camera_pb2.CaptureFrameRequest(
                stream=camera_pb2.CAMERA_STREAM_TYPE_DEPTH,
                timeout_ms=1000,
            )
        )
        assert len(depth.data) == 8 * 6 * 2
    finally:
        await channel.close()
        await server.stop()
        hardware_loop.stop()
        await camera_server.stop(0)
        camera_worker.stop()


@pytest.mark.asyncio
async def test_overhead_camera_grpc_defaults_to_color_jpeg() -> None:
    camera_worker = CameraWorker(
        lambda: SimOverheadCameraBackend(fps=60),
        role=CameraRole.OVERHEAD,
    )
    camera_server = grpc.aio.server()
    camera_pb2_grpc.add_CameraServiceServicer_to_server(
        CameraService(camera_worker),
        camera_server,
    )
    camera_port = camera_server.add_insecure_port("127.0.0.1:0")
    camera_worker.start()
    await camera_server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{camera_port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = camera_pb2_grpc.CameraServiceStub(channel)
    try:
        status = await stub.GetStatus(camera_pb2.CameraStatusRequest())
        frame = await stub.CaptureFrame(camera_pb2.CaptureFrameRequest(timeout_ms=1000))
        assert status.role == camera_pb2.CAMERA_DEVICE_ROLE_OVERHEAD
        assert len(status.profiles) == 1
        assert frame.stream == camera_pb2.CAMERA_STREAM_TYPE_COLOR
        assert frame.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_JPEG
        assert frame.role == camera_pb2.CAMERA_DEVICE_ROLE_OVERHEAD
        assert frame.captured_monotonic_ns > 0
        assert frame.data.startswith(b"\xff\xd8")
    finally:
        await channel.close()
        await camera_server.stop(0)
        camera_worker.stop()
