from __future__ import annotations

import asyncio

import grpc
import pytest
from panthera_arm import camera_pb2, camera_pb2_grpc

from armd.backend import SimBackend
from armd.camera import CameraStream, CameraWorker, SimCameraBackend
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
            if status.available:
                break
            await asyncio.sleep(0.01)
        assert status.available
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
