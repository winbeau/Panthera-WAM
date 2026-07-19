"""M1 gRPC 安全服务与统一 lease 拦截器。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import grpc
import numpy as np
from panthera_arm import arm_pb2, arm_pb2_grpc

from .backend import (
    IDLE_DAMPING_KD,
    BackendError,
    BackendLimits,
    FrameMode,
    JointFrame,
    LimitViolationError,
)
from .control import LEASE_METADATA_KEY, LeaseManager
from .execution import ExecutionRegistry
from .hardware_loop import CancelReason, HardwareLoop, MotionStepResult
from .kinematics import KinematicsWorker
from .motion import (
    CartesianTrajectoryMotion,
    JointJogMotion,
    JointMITMotion,
    JointPositionMotion,
    position_frame,
)
from .safety import apply_watchdog_stop
from .state import gripper_state_message, joint_state_message, robot_state_message

SERVICE_PREFIX = "/panthera.arm.v1.ArmService/"
DEFAULT_FRICTION_FC = np.array([0.20, 0.15, 0.15, 0.15, 0.04, 0.04], dtype=np.float64)
DEFAULT_FRICTION_FV = np.array([0.06, 0.06, 0.06, 0.03, 0.02, 0.02], dtype=np.float64)

LEASE_PROTECTED_METHODS = {
    "ReleaseControl",
    "ClearEStop",
    "SetZero",
    "JointMove",
    "MoveJ",
    "JointJog",
    "JointJogStep",
    "StopJointJog",
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

ESTOP_BLOCKED_METHODS = LEASE_PROTECTED_METHODS - {
    "ReleaseControl",
    "ClearEStop",
    "StopJointJog",
}


def finite_vector(values, *, name: str, length: int = 6) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64)
    if result.shape != (length,):
        raise ValueError(f"{name} 必须包含 {length} 个数值")
    if not np.all(np.isfinite(result)):
        raise ValueError(f"{name} 必须全部为有限数值")
    return result


def optional_double(request, field: str, default: float) -> float:
    return float(getattr(request, field)) if request.HasField(field) else default


def arm_position_reject_reason(positions: np.ndarray, limits: BackendLimits) -> str:
    below = positions < limits.joint_lower
    above = positions > limits.joint_upper
    if not np.any(below | above):
        return ""
    index = int(np.flatnonzero(below | above)[0])
    direction = "下限" if below[index] else "上限"
    limit = limits.joint_lower[index] if below[index] else limits.joint_upper[index]
    return f"joint{index + 1} 目标 {positions[index]:.6g} 超过{direction} {limit:.6g}"


def arm_magnitude_reject_reason(
    values: np.ndarray,
    limits: np.ndarray,
    *,
    label: str,
) -> str:
    exceeded = np.abs(values) > limits
    if not np.any(exceeded):
        return ""
    index = int(np.flatnonzero(exceeded)[0])
    return f"joint{index + 1} {label} {values[index]:.6g} 超过限值 ±{limits[index]:.6g}"


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
    def __init__(
        self,
        hardware_loop: HardwareLoop,
        leases: LeaseManager,
        kinematics: KinematicsWorker,
        executions: ExecutionRegistry,
    ) -> None:
        self._hardware_loop = hardware_loop
        self._leases = leases
        self._kinematics = kinematics
        self._executions = executions
        self._started_at = time.monotonic()
        self._unary_jog_motion: JointJogMotion | None = None
        self._unary_jog_completion = None
        self._unary_jog_token = ""

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
        await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.enter_idle_damping()))
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

    async def HeartbeatOnce(self, request, context):
        del request
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not token or not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        return arm_pb2.HeartbeatResponse(ok=True, server_time_ms=int(time.time() * 1000))

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

    async def SetZero(self, request, context):
        if not request.confirm:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "SetZero 必须 confirm=true")
        if self._hardware_loop.has_active_motion:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "存在活动运动，拒绝重定义零点")
        motor_ids = list(request.motor_ids) or None

        def set_zero(backend):
            states = backend.read_all()
            if len(states) != 7 or not all(state.valid for state in states):
                return False, False, "电机状态无效或连接不完整"
            moving = [state.name for state in states if abs(state.velocity) > 0.01]
            if moving:
                return False, False, f"电机尚未静止: {moving}"
            result = backend.set_zero(motor_ids)
            if result[0]:
                backend.refresh_state()
            return result

        try:
            accepted, persisted, reject_reason = await asyncio.wrap_future(
                self._hardware_loop.submit(set_zero)
            )
        except BackendError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        return arm_pb2.SetZeroResponse(
            accepted=accepted,
            persisted=persisted,
            reject_reason=reject_reason,
        )

    async def GetJointState(self, request, context):
        del request
        cached = self._hardware_loop.latest_state()
        if cached is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "尚无电机状态缓存")
        return joint_state_message(cached)

    async def GetGripperState(self, request, context):
        del request
        cached = self._hardware_loop.latest_state()
        if cached is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "尚无电机状态缓存")
        return gripper_state_message(cached)

    async def GetRobotState(self, request, context):
        del request
        cached = self._hardware_loop.latest_state()
        if cached is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "尚无电机状态缓存")
        return robot_state_message(cached, estop_engaged=self._hardware_loop.estop_engaged)

    async def StreamState(self, request, context) -> AsyncIterator[arm_pb2.RobotState]:
        rate_hz = optional_double(request, "rate_hz", 10.0)
        if rate_hz <= 0 or rate_hz > 100:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "rate_hz 必须位于 (0, 100]")
        include_joints = request.joints or not request.gripper
        include_gripper = request.gripper or not request.joints
        period_s = 1.0 / rate_hz
        try:
            while True:
                cached = self._hardware_loop.latest_state()
                if cached is None:
                    await context.abort(grpc.StatusCode.UNAVAILABLE, "尚无电机状态缓存")
                yield robot_state_message(
                    cached,
                    estop_engaged=self._hardware_loop.estop_engaged,
                    include_joints=include_joints,
                    include_gripper=include_gripper,
                )
                await asyncio.sleep(period_s)
        except asyncio.CancelledError:
            return

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

    async def CheckReached(self, request, context):
        try:
            target = finite_vector(request.target_positions, name="target_positions")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        tolerance = optional_double(request, "tolerance", 0.1)
        if tolerance < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "tolerance 不得为负数")

        def check(backend):
            backend.refresh_state()
            states = backend.read_all()
            if len(states) != 7 or not all(state.valid for state in states[:6]):
                raise BackendError("关节状态无效或连接不完整")
            errors = np.abs(target - np.array([state.position for state in states[:6]]))
            return bool(np.all(errors <= tolerance)), errors

        try:
            reached, errors = await asyncio.wrap_future(self._hardware_loop.submit(check))
        except BackendError as exc:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        return arm_pb2.CheckReachedResponse(reached=reached, errors=errors.tolist())

    async def JointMove(self, request, context):
        try:
            positions = finite_vector(request.positions, name="positions")
            velocities = finite_vector(request.velocities, name="velocities")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if np.any(velocities < 0):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "JointMove.velocities 不得为负数")
        tolerance = optional_double(request, "tolerance", 0.1)
        timeout_s = optional_double(request, "timeout_s", 15.0)
        if tolerance < 0 or timeout_s < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "tolerance/timeout_s 不得为负数")
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        max_torque = (
            finite_vector(request.max_torque, name="max_torque")
            if request.max_torque
            else limits.joint_torque
        )
        reject_reason = arm_position_reject_reason(positions, limits)
        reject_reason = reject_reason or arm_magnitude_reject_reason(
            velocities, limits.joint_velocity, label="速度"
        )
        reject_reason = reject_reason or arm_magnitude_reject_reason(
            max_torque, limits.joint_torque, label="最大力矩"
        )
        if np.any(max_torque <= 0):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "max_torque 必须全部为正数")
        if reject_reason:
            return arm_pb2.JointMoveResponse(accepted=False, reject_reason=reject_reason)
        motion = JointPositionMotion(
            positions=positions,
            velocities=velocities,
            max_torque=max_torque,
            tolerance=tolerance,
            deadline=time.monotonic() + timeout_s,
        )
        return await self._run_position_motion(motion, request.wait, arm_pb2.JointMoveResponse, context)

    async def MoveJ(self, request, context):
        try:
            positions = finite_vector(request.positions, name="positions")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if not np.isfinite(request.duration_s) or request.duration_s <= 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "duration_s 必须为正数")
        tolerance = optional_double(request, "tolerance", 0.1)
        timeout_s = optional_double(request, "timeout_s", 15.0)
        if tolerance < 0 or timeout_s < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "tolerance/timeout_s 不得为负数")
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        max_torque = (
            finite_vector(request.max_torque, name="max_torque")
            if request.max_torque
            else limits.joint_torque
        )
        reject_reason = arm_position_reject_reason(positions, limits)
        reject_reason = reject_reason or arm_magnitude_reject_reason(
            max_torque, limits.joint_torque, label="最大力矩"
        )
        if np.any(max_torque <= 0):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "max_torque 必须全部为正数")
        cached = self._hardware_loop.latest_state()
        if cached is None or not all(state.valid for state in cached.motors[:6]):
            return arm_pb2.MoveJResponse(accepted=False, reject_reason="关节状态无效或连接不完整")
        current = np.array([state.position for state in cached.motors[:6]], dtype=np.float64)
        velocities = np.abs(positions - current) / request.duration_s
        reject_reason = reject_reason or arm_magnitude_reject_reason(
            velocities, limits.joint_velocity, label="计算速度"
        )
        if reject_reason:
            return arm_pb2.MoveJResponse(accepted=False, reject_reason=reject_reason)
        motion = JointPositionMotion(
            positions=positions,
            velocities=velocities,
            max_torque=max_torque,
            tolerance=tolerance,
            deadline=time.monotonic() + timeout_s,
        )
        return await self._run_position_motion(motion, request.wait, arm_pb2.MoveJResponse, context)

    async def JointJog(self, request_iterator, context) -> AsyncIterator[arm_pb2.JointJogFeedback]:
        iterator = request_iterator.__aiter__()
        try:
            first = await iterator.__anext__()
        except StopAsyncIteration:
            return
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        try:
            first_velocities = finite_vector(first.velocities, name="velocities")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        reject_reason = arm_magnitude_reject_reason(first_velocities, limits.joint_velocity, label="速度")
        if reject_reason:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, reject_reason)

        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        motion = JointJogMotion()
        motion.update(first_velocities)
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

        async def consume_commands() -> None:
            async for command in iterator:
                velocities = finite_vector(command.velocities, name="velocities")
                reason = arm_magnitude_reject_reason(velocities, limits.joint_velocity, label="速度")
                if reason:
                    raise ValueError(reason)
                if not self._leases.heartbeat(token):
                    raise PermissionError("控制权 lease 已失效")
                motion.update(velocities)

        consumer = asyncio.create_task(consume_commands(), name="panthera-joint-jog-consumer")
        try:
            while not completion.done():
                if consumer.done():
                    error = consumer.exception()
                    if isinstance(error, ValueError):
                        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
                    if isinstance(error, PermissionError):
                        await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(error))
                    break
                cached = self._hardware_loop.latest_state()
                if cached is not None:
                    yield arm_pb2.JointJogFeedback(
                        joint_state=joint_state_message(cached),
                        limit_hit=motion.limit_hit,
                    )
                await asyncio.sleep(0.05)
        finally:
            motion.request_cancel(CancelReason.CLIENT)
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            try:
                await asyncio.wait_for(asyncio.wrap_future(completion), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError, RuntimeError):
                pass

    async def JointMIT(self, request_iterator, context) -> AsyncIterator[arm_pb2.JointMITFeedback]:
        iterator = request_iterator.__aiter__()
        try:
            first = await iterator.__anext__()
        except StopAsyncIteration:
            return
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        try:
            first_values = self._mit_values(first, limits)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        motion = JointMITMotion()
        motion.update(**first_values)
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

        async def consume_commands() -> None:
            async for command in iterator:
                values = self._mit_values(command, limits)
                if not self._leases.heartbeat(token):
                    raise PermissionError("控制权 lease 已失效")
                motion.update(**values)

        consumer = asyncio.create_task(consume_commands(), name="panthera-joint-mit-consumer")
        try:
            while not completion.done():
                if consumer.done():
                    error = consumer.exception()
                    if isinstance(error, ValueError):
                        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
                    if isinstance(error, PermissionError):
                        await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(error))
                    break
                cached = self._hardware_loop.latest_state()
                if cached is not None:
                    yield arm_pb2.JointMITFeedback(joint_state=joint_state_message(cached))
                await asyncio.sleep(0.05)
        finally:
            motion.request_cancel(CancelReason.CLIENT)
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            try:
                await asyncio.wait_for(asyncio.wrap_future(completion), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError, RuntimeError):
                pass

    async def JointJogStep(self, request, context):
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        try:
            velocities = finite_vector(request.velocities, name="velocities")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        reject_reason = arm_magnitude_reject_reason(velocities, limits.joint_velocity, label="速度")
        if reject_reason:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, reject_reason)

        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        completion = self._unary_jog_completion
        if completion is not None and completion.done():
            self._clear_unary_jog()
            completion = None
        if completion is None:
            motion = JointJogMotion()
            motion.update(velocities)
            accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
            try:
                await asyncio.wrap_future(accepted)
            except RuntimeError as exc:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            self._unary_jog_motion = motion
            self._unary_jog_completion = completion
            self._unary_jog_token = token
        elif token != self._unary_jog_token or self._unary_jog_motion is None:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "点动控制权已变更")
        else:
            self._unary_jog_motion.update(velocities)

        cached = self._hardware_loop.latest_state()
        if cached is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "尚无电机状态缓存")
        return arm_pb2.JointJogFeedback(
            joint_state=joint_state_message(cached),
            limit_hit=self._unary_jog_motion.limit_hit if self._unary_jog_motion else (),
        )

    async def StopJointJog(self, request, context):
        del request
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        motion = self._unary_jog_motion
        completion = self._unary_jog_completion
        if motion is None or completion is None or token != self._unary_jog_token:
            return arm_pb2.Empty()
        motion.request_cancel(CancelReason.CLIENT)
        try:
            await asyncio.wait_for(asyncio.wrap_future(completion), timeout=0.5)
        except (TimeoutError, asyncio.TimeoutError, RuntimeError):
            pass
        self._clear_unary_jog()
        return arm_pb2.Empty()

    def _clear_unary_jog(self) -> None:
        self._unary_jog_motion = None
        self._unary_jog_completion = None
        self._unary_jog_token = ""

    async def GripperMove(self, request, context):
        return await self._gripper_move(
            position=request.position,
            velocity=request.velocity,
            max_torque=optional_double(request, "max_torque", 0.5),
            context=context,
        )

    async def GripperOpen(self, request, context):
        return await self._gripper_move(
            position=optional_double(request, "position", 1.6),
            velocity=optional_double(request, "velocity", 0.5),
            max_torque=optional_double(request, "max_torque", 0.5),
            context=context,
        )

    async def GripperClose(self, request, context):
        return await self._gripper_move(
            position=optional_double(request, "position", 0.0),
            velocity=optional_double(request, "velocity", 0.5),
            max_torque=optional_double(request, "max_torque", 0.5),
            context=context,
        )

    async def GripperMIT(self, request, context):
        values = np.array(
            [request.position, request.velocity, request.torque, request.kp, request.kd],
            dtype=np.float64,
        )
        if not np.all(np.isfinite(values)):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "夹爪 MIT 参数必须为有限数值")
        if request.kp < 0 or request.kd < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "夹爪 MIT kp/kd 不得为负数")
        if self._hardware_loop.has_active_motion:
            return arm_pb2.GripperMITResponse(accepted=False, reject_reason="已有运动正在执行")

        def command(backend):
            limits = backend.limits
            if request.position < limits.gripper_lower or request.position > limits.gripper_upper:
                return False, (
                    f"gripper 目标 {request.position:.6g} 超出"
                    f"[{limits.gripper_lower:.6g}, {limits.gripper_upper:.6g}]"
                )
            if abs(request.velocity) > limits.gripper_velocity:
                return False, f"gripper 速度超过限值 ±{limits.gripper_velocity:.6g}"
            if abs(request.torque) > limits.gripper_torque:
                return False, f"gripper 前馈力矩超过限值 ±{limits.gripper_torque:.6g}"
            states = backend.read_all()
            if len(states) != 7 or not all(state.valid for state in states):
                return False, "电机状态无效或连接不完整"
            backend.write_frame(
                JointFrame(
                    mode=FrameMode.POS_VEL_TQE_KP_KD,
                    arm_position=np.array(
                        [state.position for state in states[:6]],
                        dtype=np.float64,
                    ),
                    arm_velocity=np.zeros(6),
                    arm_torque=np.zeros(6),
                    arm_kp=np.zeros(6),
                    arm_kd=IDLE_DAMPING_KD,
                    gripper_position=request.position,
                    gripper_velocity=request.velocity,
                    gripper_torque=request.torque,
                    gripper_kp=request.kp,
                    gripper_kd=request.kd,
                )
            )
            return True, ""

        try:
            accepted, reason = await asyncio.wrap_future(self._hardware_loop.submit(command))
        except (BackendError, LimitViolationError, ValueError) as exc:
            return arm_pb2.GripperMITResponse(accepted=False, reject_reason=str(exc))
        return arm_pb2.GripperMITResponse(accepted=accepted, reject_reason=reason)

    async def GetForwardKinematics(self, request, context):
        q = await self._request_joint_angles(request.joint_angles, context)
        result = await self._kinematics.call("fk", {"q": q})
        return arm_pb2.ForwardKinematicsResponse(
            position=np.asarray(result["position"]).tolist(),
            rotation_matrix=np.asarray(result["rotation"]).reshape(-1).tolist(),
            transform=np.asarray(result["transform"]).reshape(-1).tolist(),
            used_joint_angles=q.tolist(),
        )

    async def GetJacobian(self, request, context):
        q = await self._request_joint_angles(request.joint_angles, context)
        matrix = np.asarray(await self._kinematics.call("jacobian", {"q": q}))
        return arm_pb2.JacobianResponse(
            matrix=matrix.reshape(-1).tolist(),
            rows=matrix.shape[0],
            cols=matrix.shape[1],
        )

    async def GetManipulability(self, request, context):
        q = await self._request_joint_angles(request.joint_angles, context)
        value = await self._kinematics.call("manipulability", {"q": q})
        return arm_pb2.ManipulabilityResponse(mu=float(value))

    async def GetDynamicsTerm(self, request, context):
        terms = {
            arm_pb2.DYNAMICS_TERM_GRAVITY: "gravity",
            arm_pb2.DYNAMICS_TERM_CORIOLIS: "coriolis",
            arm_pb2.DYNAMICS_TERM_MASS_MATRIX: "mass_matrix",
            arm_pb2.DYNAMICS_TERM_INERTIA: "inertia",
            arm_pb2.DYNAMICS_TERM_FULL_INVERSE_DYNAMICS: "inverse_dynamics",
            arm_pb2.DYNAMICS_TERM_FRICTION: "friction",
        }
        term = terms.get(request.term)
        if term is None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "必须指定有效的 dynamics term")
        cached = self._hardware_loop.latest_state()
        if cached is None or not all(state.valid for state in cached.motors[:6]):
            await context.abort(grpc.StatusCode.UNAVAILABLE, "关节状态无效或连接不完整")
        current_q = np.array([state.position for state in cached.motors[:6]], dtype=np.float64)
        current_v = np.array([state.velocity for state in cached.motors[:6]], dtype=np.float64)
        try:
            q = finite_vector(request.q, name="q") if request.q else current_q
            v = finite_vector(request.v, name="v") if request.v else current_v
            a = finite_vector(request.a, name="a") if request.a else np.zeros(6)
            fc = finite_vector(request.fc, name="fc") if request.fc else DEFAULT_FRICTION_FC
            fv = finite_vector(request.fv, name="fv") if request.fv else DEFAULT_FRICTION_FV
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        vel_threshold = optional_double(request, "vel_threshold", 0.01)
        if vel_threshold < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "vel_threshold 不得为负数")
        try:
            result = await self._kinematics.call(
                "dynamics",
                {
                    "term": term,
                    "q": q,
                    "v": v,
                    "a": a,
                    "fc": fc,
                    "fv": fv,
                    "vel_threshold": vel_threshold,
                },
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        response = arm_pb2.DynamicsQueryResponse()
        for field, values in result.items():
            getattr(response, field).extend(np.asarray(values).reshape(-1).tolist())
        return response

    async def GetInverseKinematics(self, request, context):
        current = await self._request_joint_angles(request.init_q, context)
        try:
            target_position, target_rotation = self._pose_values(request.target)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        timeout_s = optional_double(request, "timeout_s", 0.5)
        if timeout_s < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "timeout_s 不得为负数")
        payload = {
            "target_position": target_position,
            "target_rotation": target_rotation,
            "init_q": current,
            "max_iter": request.max_iter if request.HasField("max_iter") else 1000,
            "eps": optional_double(request, "eps", 1e-3),
            "damping": optional_double(request, "damping", 1e-2),
            "adaptive_damping": (request.adaptive_damping if request.HasField("adaptive_damping") else True),
            "multi_init": request.multi_init if request.HasField("multi_init") else True,
            "num_attempts": request.num_attempts if request.HasField("num_attempts") else 8,
        }
        if payload["max_iter"] <= 0 or payload["eps"] < 0 or payload["damping"] < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "IK 迭代数必须为正，eps/damping 不得为负")
        if payload["num_attempts"] <= 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "num_attempts 必须为正数")
        await self._kinematics.warm()
        try:
            result = await asyncio.wait_for(self._kinematics.call("ik", payload), timeout=timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            return arm_pb2.InverseKinematicsResponse(found=False, timeout=True)
        if result is None:
            return arm_pb2.InverseKinematicsResponse(found=False, timeout=False)
        joint_angles = np.asarray(result, dtype=np.float64)
        fk = await self._kinematics.call("fk", {"q": joint_angles})
        error = float(np.linalg.norm(np.asarray(fk["position"]) - target_position))
        return arm_pb2.InverseKinematicsResponse(
            found=True,
            joint_angles=joint_angles.tolist(),
            error=error,
            timeout=False,
        )

    async def PlanCartesianPath(self, request, context):
        current = await self._request_joint_angles([], context)
        current_fk = await self._kinematics.call("fk", {"q": current})
        try:
            waypoints = self._waypoint_values(request.waypoints, current_fk)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        try:
            result = await self._kinematics.call(
                "plan",
                {
                    "current_q": current,
                    "waypoints": waypoints,
                    "duration": None,
                    "use_spline": True,
                },
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        return self._plan_response(result)

    async def MoveL(self, request, context):
        if self._hardware_loop.has_active_motion:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "已有运动正在执行")
        current = await self._request_joint_angles([], context)
        current_fk = await self._kinematics.call("fk", {"q": current})
        try:
            target_position, target_rotation = self._pose_values(
                request.target,
                default_rotation=np.asarray(current_fk["rotation"]),
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        duration = optional_double(request, "duration_s", 0.0) if request.HasField("duration_s") else None
        if duration is not None and duration <= 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "duration_s 必须为正数")
        use_spline = request.use_spline if request.HasField("use_spline") else True
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        try:
            max_torque = (
                finite_vector(request.max_torque, name="max_torque")
                if request.max_torque
                else limits.joint_torque
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        reason = arm_magnitude_reject_reason(max_torque, limits.joint_torque, label="最大力矩")
        if reason or np.any(max_torque <= 0):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                reason or "max_torque 必须全部为正数",
            )
        try:
            result = await self._kinematics.call(
                "plan",
                {
                    "current_q": current,
                    "waypoints": [
                        {
                            "position": np.asarray(current_fk["position"]),
                            "rotation": np.asarray(current_fk["rotation"]),
                        },
                        {"position": target_position, "rotation": target_rotation},
                    ],
                    "duration": duration,
                    "use_spline": use_spline,
                },
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if result["fraction"] < 0.999 or not result["positions"]:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"笛卡尔路径仅完成 {result['fraction'] * 100:.1f}%",
            )
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        motion = CartesianTrajectoryMotion(
            positions=[np.asarray(value) for value in result["positions"]],
            velocities=[np.asarray(value) for value in result["velocities"]],
            timestamps=list(result["timestamps"]),
            max_torque=max_torque,
        )
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        execution_id = self._executions.register(motion, completion)
        return arm_pb2.ExecutionAccepted(execution_id=execution_id)

    async def StreamExecution(self, request, context) -> AsyncIterator[arm_pb2.ExecutionStatus]:
        while True:
            snapshot = self._executions.snapshot(request.execution_id)
            if snapshot is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, "execution_id 不存在")
            cached = self._hardware_loop.latest_state()
            response = arm_pb2.ExecutionStatus(
                execution_id=snapshot.execution_id,
                state=self._execution_state(snapshot.result),
                fraction=snapshot.fraction,
                error_message=snapshot.error_message,
            )
            if cached is not None:
                response.robot_state.CopyFrom(
                    robot_state_message(cached, estop_engaged=self._hardware_loop.estop_engaged)
                )
            yield response
            if snapshot.terminal:
                return
            await asyncio.sleep(0.05)

    async def CancelExecution(self, request, context):
        cancelled = self._executions.cancel(request.execution_id)
        return arm_pb2.CancelExecutionResponse(cancelled=cancelled)

    async def _run_position_motion(self, motion, wait, response_type, context):
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            return response_type(accepted=False, reject_reason=str(exc))
        if not wait:
            return response_type(accepted=True, reached=False)
        try:
            result = await asyncio.wrap_future(completion)
        except asyncio.CancelledError:
            motion.request_cancel(CancelReason.CLIENT)
            raise
        except (BackendError, LimitViolationError, RuntimeError, ValueError) as exc:
            return response_type(accepted=False, reject_reason=str(exc))
        return response_type(
            accepted=True,
            reached=result is MotionStepResult.DONE,
            errors=motion.errors.tolist(),
            reject_reason=motion.reject_reason,
        )

    async def _gripper_move(self, *, position, velocity, max_torque, context):
        values = np.array([position, velocity, max_torque], dtype=np.float64)
        if not np.all(np.isfinite(values)):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "夹爪参数必须为有限数值")
        if velocity < 0 or max_torque <= 0:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "夹爪 velocity 不得为负且 max_torque 必须为正"
            )
        if self._hardware_loop.has_active_motion:
            return arm_pb2.GripperMoveResponse(accepted=False, reject_reason="已有运动正在执行")

        def command(backend):
            limits = backend.limits
            if position < limits.gripper_lower:
                return False, f"gripper 目标 {position:.6g} 超过下限 {limits.gripper_lower:.6g}"
            if position > limits.gripper_upper:
                return False, f"gripper 目标 {position:.6g} 超过上限 {limits.gripper_upper:.6g}"
            if velocity > limits.gripper_velocity:
                return False, f"gripper 速度 {velocity:.6g} 超过限值 {limits.gripper_velocity:.6g}"
            if max_torque > limits.gripper_torque:
                return False, f"gripper 最大力矩 {max_torque:.6g} 超过限值 {limits.gripper_torque:.6g}"
            states = backend.read_all()
            if len(states) != 7 or not all(state.valid for state in states):
                return False, "电机状态无效或连接不完整"
            backend.write_frame(
                position_frame(
                    backend,
                    arm_position=np.array([state.position for state in states[:6]], dtype=np.float64),
                    arm_velocity=np.full(6, 0.1),
                    gripper_position=position,
                    gripper_velocity=velocity,
                    gripper_max_torque=max_torque,
                )
            )
            return True, ""

        try:
            accepted, reject_reason = await asyncio.wrap_future(self._hardware_loop.submit(command))
        except (BackendError, LimitViolationError, ValueError) as exc:
            return arm_pb2.GripperMoveResponse(accepted=False, reject_reason=str(exc))
        return arm_pb2.GripperMoveResponse(accepted=accepted, reject_reason=reject_reason)

    async def _request_joint_angles(self, values, context) -> np.ndarray:
        if values:
            try:
                return finite_vector(values, name="joint_angles")
            except ValueError as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        cached = self._hardware_loop.latest_state()
        if cached is None or not all(state.valid for state in cached.motors[:6]):
            await context.abort(grpc.StatusCode.UNAVAILABLE, "关节状态无效或连接不完整")
        return np.array([state.position for state in cached.motors[:6]], dtype=np.float64)

    @staticmethod
    def _mit_values(command, limits: BackendLimits) -> dict[str, np.ndarray]:
        positions = finite_vector(command.positions, name="positions")
        velocities = finite_vector(command.velocities, name="velocities")
        torques = finite_vector(command.torques, name="torques")
        kp = finite_vector(command.kp, name="kp")
        kd = finite_vector(command.kd, name="kd")
        reason = arm_position_reject_reason(positions, limits)
        reason = reason or arm_magnitude_reject_reason(
            velocities,
            limits.joint_velocity,
            label="速度",
        )
        reason = reason or arm_magnitude_reject_reason(
            torques,
            limits.joint_torque,
            label="前馈力矩",
        )
        if reason:
            raise ValueError(reason)
        if np.any(kp < 0) or np.any(kd < 0):
            raise ValueError("MIT kp/kd 不得为负数")
        return {
            "positions": positions,
            "velocities": velocities,
            "torques": torques,
            "kp": kp,
            "kd": kd,
        }

    @staticmethod
    def _pose_values(pose, default_rotation: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        position = finite_vector(pose.position, name="CartesianPose.position", length=3)
        orientation = pose.WhichOneof("orientation")
        if orientation == "rpy":
            from scipy.spatial.transform import Rotation

            rotation = Rotation.from_euler(
                "xyz",
                [pose.rpy.roll, pose.rpy.pitch, pose.rpy.yaw],
            ).as_matrix()
        elif orientation == "matrix":
            rotation = finite_vector(pose.matrix.values, name="RotationMatrix.values", length=9).reshape(3, 3)
        elif default_rotation is not None:
            rotation = np.asarray(default_rotation, dtype=np.float64)
        else:
            rotation = np.eye(3)
        return position, rotation

    def _waypoint_values(self, waypoints, current_fk) -> list[dict[str, np.ndarray]]:
        if not waypoints:
            raise ValueError("waypoints 不能为空")
        result: list[dict[str, np.ndarray]] = []
        default_rotation = np.asarray(current_fk["rotation"])
        if len(waypoints) == 1:
            result.append(
                {
                    "position": np.asarray(current_fk["position"]),
                    "rotation": default_rotation,
                }
            )
        for waypoint in waypoints:
            position, rotation = self._pose_values(waypoint, default_rotation=default_rotation)
            result.append({"position": position, "rotation": rotation})
            default_rotation = rotation
        return result

    @staticmethod
    def _plan_response(result) -> arm_pb2.PlanCartesianPathResponse:
        response = arm_pb2.PlanCartesianPathResponse(fraction=float(result["fraction"]))
        for positions, velocities, timestamp in zip(
            result["positions"],
            result["velocities"],
            result["timestamps"],
            strict=True,
        ):
            response.joint_trajectory.add(
                positions=np.asarray(positions).tolist(),
                velocities=np.asarray(velocities).tolist(),
                timestamp_s=float(timestamp),
            )
        return response

    @staticmethod
    def _execution_state(result: MotionStepResult) -> int:
        return {
            MotionStepResult.RUNNING: arm_pb2.EXEC_STATE_RUNNING,
            MotionStepResult.DONE: arm_pb2.EXEC_STATE_DONE,
            MotionStepResult.FAILED: arm_pb2.EXEC_STATE_FAILED,
            MotionStepResult.CANCELLED: arm_pb2.EXEC_STATE_CANCELLED,
        }[result]
