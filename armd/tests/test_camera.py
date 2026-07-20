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
    CameraStream,
    CameraWorker,
    SimCameraBackend,
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
        timeout=10,
    )
    assert json.loads(result.stdout) == {
        "available": True,
        "streaming": True,
        "model": "RealSense D405 Simulator",
        "width": 8,
        "height": 6,
        "bytes": 96,
    }


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

        depth = await stub.CaptureFrame(
            camera_pb2.CaptureFrameRequest(
                stream=camera_pb2.CAMERA_STREAM_TYPE_DEPTH,
                timeout_ms=1000,
            )
        )
        assert depth.pixel_format == camera_pb2.CAMERA_PIXEL_FORMAT_Z16
        assert len(depth.data) == 8 * 6 * 2

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
