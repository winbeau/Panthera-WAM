"""armd 异步 gRPC server 生命周期。"""

from __future__ import annotations

import asyncio

import grpc
from panthera_arm import arm_pb2_grpc, camera_pb2_grpc

from .camera.backend import CameraWorker
from .camera.service import CameraProxyService, CameraService
from .control import LeaseManager
from .execution import ExecutionRegistry
from .grpc_service import ArmService, SafetyInterceptor
from .hardware_loop import CancelReason, HardwareLoop
from .kinematics import KinematicsWorker
from .safety import apply_watchdog_stop


class ArmdServer:
    def __init__(
        self,
        hardware_loop: HardwareLoop,
        *,
        bind: str = "127.0.0.1:50051",
        lease_timeout_s: float = 2.0,
        watchdog_poll_s: float = 0.05,
        sdk_root: str | None = None,
        config_path: str | None = None,
        camera_worker: CameraWorker | None = None,
        camera_endpoint: str | None = None,
    ) -> None:
        if camera_worker is not None and camera_endpoint is not None:
            raise ValueError("camera_worker 与 camera_endpoint 不能同时设置")
        self.hardware_loop = hardware_loop
        self.bind = bind
        self.leases = LeaseManager(timeout_s=lease_timeout_s)
        self.executions = ExecutionRegistry()
        self.kinematics = KinematicsWorker(sdk_root=sdk_root, config_path=config_path)
        self.camera_worker = camera_worker
        self.camera_proxy = CameraProxyService(camera_endpoint) if camera_endpoint else None
        self._watchdog_poll_s = watchdog_poll_s
        self._server = grpc.aio.server(interceptors=[SafetyInterceptor(self.leases, hardware_loop)])
        arm_pb2_grpc.add_ArmServiceServicer_to_server(
            ArmService(hardware_loop, self.leases, self.kinematics, self.executions),
            self._server,
        )
        camera_pb2_grpc.add_CameraServiceServicer_to_server(
            self.camera_proxy or CameraService(camera_worker),
            self._server,
        )
        self.port = self._server.add_insecure_port(bind)
        if self.port == 0:
            raise RuntimeError(f"无法监听 gRPC 地址: {bind}")
        self._watchdog_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.camera_worker is not None:
            self.camera_worker.start()
        await self._server.start()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="panthera-watchdog")

    async def stop(self, grace: float = 0.0) -> None:
        task = self._watchdog_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        await self._server.stop(grace)
        if self.camera_worker is not None:
            self.camera_worker.stop()
        if self.camera_proxy is not None:
            await self.camera_proxy.close()
        self.kinematics.close()

    async def wait_for_termination(self) -> None:
        await self._server.wait_for_termination()

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self._watchdog_poll_s)
            expired = self.leases.expire_if_stale()
            if expired is None:
                continue
            if self.hardware_loop.has_active_motion:
                self.hardware_loop.request_cancel(CancelReason.WATCHDOG)
            await asyncio.wrap_future(self.hardware_loop.submit(apply_watchdog_stop))
