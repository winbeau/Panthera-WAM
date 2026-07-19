"""D405 CameraService gRPC 适配层。"""

from __future__ import annotations

import asyncio
import time

import grpc
from panthera_arm import camera_pb2, camera_pb2_grpc

from .backend import CameraFrameSnapshot, CameraPixelFormat, CameraStream, CameraWorker


def camera_stream(value: int) -> CameraStream:
    if value in (
        camera_pb2.CAMERA_STREAM_TYPE_UNSPECIFIED,
        camera_pb2.CAMERA_STREAM_TYPE_DEPTH,
    ):
        return CameraStream.DEPTH
    if value == camera_pb2.CAMERA_STREAM_TYPE_COLOR:
        return CameraStream.COLOR
    raise ValueError(f"未知相机流类型: {value}")


def stream_message(stream: CameraStream) -> int:
    return {
        CameraStream.DEPTH: camera_pb2.CAMERA_STREAM_TYPE_DEPTH,
        CameraStream.COLOR: camera_pb2.CAMERA_STREAM_TYPE_COLOR,
    }[stream]


def pixel_format_message(pixel_format: CameraPixelFormat) -> int:
    return {
        CameraPixelFormat.Z16: camera_pb2.CAMERA_PIXEL_FORMAT_Z16,
        CameraPixelFormat.RGB8: camera_pb2.CAMERA_PIXEL_FORMAT_RGB8,
    }[pixel_format]


def frame_message(frame: CameraFrameSnapshot) -> camera_pb2.CameraFrame:
    return camera_pb2.CameraFrame(
        stream=stream_message(frame.stream),
        pixel_format=pixel_format_message(frame.pixel_format),
        sequence=frame.sequence,
        captured_at_ns=frame.captured_at_ns,
        device_timestamp_ms=frame.device_timestamp_ms,
        width=frame.width,
        height=frame.height,
        stride=frame.stride,
        depth_scale=frame.depth_scale,
        data=frame.data,
    )


class CameraService(camera_pb2_grpc.CameraServiceServicer):
    def __init__(self, worker: CameraWorker | None) -> None:
        self._worker = worker

    async def GetStatus(self, request, context):
        del request, context
        worker = self._worker
        if worker is None:
            return camera_pb2.CameraStatus(enabled=False, error="相机采集未启用")
        status = worker.status()
        response = camera_pb2.CameraStatus(
            enabled=status.enabled,
            available=status.available,
            streaming=status.streaming,
            model=status.model,
            serial=status.serial,
            firmware=status.firmware,
            usb_type=status.usb_type,
            sdk_version=status.sdk_version,
            error=status.error,
            last_frame_age_ms=status.last_frame_age_ms,
            actual_fps=status.actual_fps,
        )
        for profile in status.profiles:
            response.profiles.add(
                stream=stream_message(profile.stream),
                pixel_format=pixel_format_message(profile.pixel_format),
                width=profile.width,
                height=profile.height,
                fps=profile.fps,
            )
        return response

    async def CaptureFrame(self, request, context):
        worker = await self._require_worker(context)
        try:
            stream = camera_stream(request.stream)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        timeout_ms = request.timeout_ms or 5000
        if not 100 <= timeout_ms <= 10000:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "timeout_ms 必须在 100–10000 之间")
        frame = await asyncio.to_thread(worker.wait_for_frame, stream, timeout_s=timeout_ms / 1000)
        if frame is None:
            status = worker.status()
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                status.error or f"{stream.value} 流在 {timeout_ms}ms 内没有新帧",
            )
        return frame_message(frame)

    async def StreamFrames(self, request, context):
        worker = await self._require_worker(context)
        try:
            stream = camera_stream(request.stream)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        max_rate_hz = request.max_rate_hz or 30.0
        if not 0.1 <= max_rate_hz <= 90.0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "max_rate_hz 必须在 0.1–90 之间")
        if request.max_frames < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "max_frames 不能为负数")

        last_sequence = 0
        sent = 0
        interval_s = 1.0 / max_rate_hz
        next_emit_at = time.monotonic()
        try:
            while request.max_frames == 0 or sent < request.max_frames:
                delay = next_emit_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                frame = await asyncio.to_thread(
                    worker.wait_for_frame,
                    stream,
                    after_sequence=last_sequence,
                    timeout_s=2.0,
                )
                if frame is None:
                    status = worker.status()
                    if not status.available:
                        await context.abort(
                            grpc.StatusCode.UNAVAILABLE,
                            status.error or "D405 当前不可用",
                        )
                    continue
                yield frame_message(frame)
                last_sequence = frame.sequence
                sent += 1
                next_emit_at = max(next_emit_at + interval_s, time.monotonic())
        except asyncio.CancelledError:
            return

    async def _require_worker(self, context) -> CameraWorker:
        if self._worker is None:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "相机采集未启用")
        return self._worker
