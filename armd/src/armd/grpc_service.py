"""M1 gRPC 安全服务与统一 lease 拦截器。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import grpc
from panthera_arm import arm_pb2, arm_pb2_grpc

from .control import LEASE_METADATA_KEY, LeaseManager
from .hardware_loop import CancelReason, HardwareLoop
from .safety import apply_watchdog_stop

SERVICE_PREFIX = "/panthera.arm.v1.ArmService/"

LEASE_PROTECTED_METHODS = {
    "ReleaseControl",
    "ClearEStop",
    "SetZero",
    "JointMove",
    "MoveJ",
    "JointJog",
    "JointMIT",
    "GripperMove",
    "GripperOpen",
    "GripperClose",
    "GripperMIT",
    "MoveL",
    "CartesianJog",
    "RunJointTrajectory",
    "TeachStart",
    "TeachStop",
    "TeachRecordStart",
    "TeachRecordStop",
    "TeachPlay",
    "CancelExecution",
}

ESTOP_BLOCKED_METHODS = LEASE_PROTECTED_METHODS - {"ReleaseControl", "ClearEStop"}


def metadata_value(metadata, key: str) -> str:
    for item in metadata:
        if item.key == key:
            return item.value
    return ""


class SafetyInterceptor(grpc.aio.ServerInterceptor):
    def __init__(self, leases: LeaseManager, hardware_loop: HardwareLoop) -> None:
        self._leases = leases
        self._hardware_loop = hardware_loop

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None or not handler_call_details.method.startswith(SERVICE_PREFIX):
            return handler
        method_name = handler_call_details.method.removeprefix(SERVICE_PREFIX)
        if method_name not in LEASE_PROTECTED_METHODS:
            return handler

        async def authorize(context: grpc.aio.ServicerContext) -> None:
            token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
            if not token or not self._leases.validate(token):
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "缺少或无效的控制权 lease")
            if method_name in ESTOP_BLOCKED_METHODS and self._hardware_loop.estop_engaged:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "EStop 已触发，运动类 RPC 被拒绝")

        if handler.unary_unary:

            async def unary_unary(request, context):
                await authorize(context)
                return await handler.unary_unary(request, context)

            return grpc.unary_unary_rpc_method_handler(
                unary_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.unary_stream:

            async def unary_stream(request, context):
                await authorize(context)
                async for response in handler.unary_stream(request, context):
                    yield response

            return grpc.unary_stream_rpc_method_handler(
                unary_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_unary:

            async def stream_unary(request_iterator, context):
                await authorize(context)
                return await handler.stream_unary(request_iterator, context)

            return grpc.stream_unary_rpc_method_handler(
                stream_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_stream:

            async def stream_stream(request_iterator, context):
                await authorize(context)
                async for response in handler.stream_stream(request_iterator, context):
                    yield response

            return grpc.stream_stream_rpc_method_handler(
                stream_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        return handler


class ArmService(arm_pb2_grpc.ArmServiceServicer):
    def __init__(self, hardware_loop: HardwareLoop, leases: LeaseManager) -> None:
        self._hardware_loop = hardware_loop
        self._leases = leases
        self._started_at = time.monotonic()

    async def AcquireControl(self, request, context):
        try:
            result = self._leases.acquire(request.client_id, force=request.force)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if result.replaced_holder:
            if self._hardware_loop.has_active_motion:
                self._hardware_loop.request_cancel(CancelReason.FORCE_ACQUIRE)
            await asyncio.wrap_future(self._hardware_loop.submit(apply_watchdog_stop))
        return arm_pb2.AcquireControlResponse(
            granted=result.granted,
            holder_client_id=result.holder_client_id,
            lease_token=result.token,
        )

    async def ReleaseControl(self, request, context):
        del request
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.release(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        return arm_pb2.Empty()

    async def GetControlStatus(self, request, context):
        del request, context
        snapshot = self._leases.snapshot()
        return arm_pb2.ControlStatus(
            held=snapshot.held,
            holder_client_id=snapshot.holder_client_id,
            estop_engaged=self._hardware_loop.estop_engaged,
            watchdog_ok=snapshot.watchdog_ok,
            last_heartbeat_age_ms=round(snapshot.heartbeat_age_s * 1000),
        )

    async def Heartbeat(self, request_iterator, context) -> AsyncIterator[arm_pb2.HeartbeatResponse]:
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        async for _ in request_iterator:
            if not token or not self._leases.heartbeat(token):
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
            yield arm_pb2.HeartbeatResponse(ok=True, server_time_ms=int(time.time() * 1000))

    async def EStop(self, request, context):
        del request
        self._hardware_loop.request_estop()
        deadline = time.monotonic() + 0.2
        while not self._hardware_loop.estop_applied and time.monotonic() < deadline:
            await asyncio.sleep(min(self._hardware_loop.period_s, 0.005))
        if not self._hardware_loop.estop_applied:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "EStop 未在 200ms 内由 HardwareLoop 执行")
        return arm_pb2.EStopResponse(engaged=True, timestamp_ms=int(time.time() * 1000))

    async def ClearEStop(self, request, context):
        if not request.confirm:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "ClearEStop 必须 confirm=true")
        if not self._hardware_loop.clear_estop():
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "EStop 尚未执行或未处于触发态")
        return arm_pb2.EStopResponse(engaged=False, timestamp_ms=int(time.time() * 1000))

    async def GetSoftLimits(self, request, context):
        del request, context
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        response = arm_pb2.SoftLimits(hardware_limits_enabled=False)
        for index in range(6):
            response.joint_limits.add(
                name=f"joint{index + 1}",
                pos_min=limits.joint_lower[index],
                pos_max=limits.joint_upper[index],
                vel_max=limits.joint_velocity[index],
                torque_max=limits.joint_torque[index],
            )
        response.gripper_limit.CopyFrom(
            arm_pb2.GripperLimit(
                pos_min=limits.gripper_lower,
                pos_max=limits.gripper_upper,
                vel_max=limits.gripper_velocity,
                torque_max=limits.gripper_torque,
            )
        )
        return response

    async def GetDaemonStatus(self, request, context):
        del request, context
        is_sim, sdk_version, estop_latch_hazard_present = await asyncio.wrap_future(
            self._hardware_loop.submit(
                lambda backend: (
                    backend.is_sim,
                    backend.sdk_version,
                    backend.estop_latch_hazard_present,
                )
            )
        )
        state = self._hardware_loop.latest_state()
        return arm_pb2.DaemonStatus(
            version="0.1.0",
            sim=is_sim,
            control_hz=self._hardware_loop.stats().actual_hz,
            uptime_ms=round((time.monotonic() - self._started_at) * 1000),
            sdk_version=sdk_version,
            estop_latch_hazard_present=estop_latch_hazard_present,
            hardware_connected=state is not None and all(motor.valid for motor in state.motors),
        )

    async def JointMove(self, request, context):
        del request
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "JointMove 将在 M3 实现")
