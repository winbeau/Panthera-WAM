"""M1 gRPC 安全服务与统一 lease 拦截器。"""

from __future__ import annotations

import asyncio
import logging
import os
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
    GripperPositionMotion,
    JointJogMotion,
    JointMITMotion,
    JointPositionMotion,
    TEACH_TAU_LIMIT,
    TeachClutchCommand,
    TeachMotion,
    TeachPlaybackMotion,
    AutoHoldConfig,
)
from .policy import (
    PolicyChunkMotion,
    PolicySafetyConfig,
    PolicyValidationError,
    validate_policy_chunk,
)
from .policy_assets import PolicyAssetAllowList
from .policy_confirmation import PolicyConfirmationManager
from .safety import apply_watchdog_stop
from .state import gripper_state_message, joint_state_message, robot_state_message
from .state_tap import StateTapDataLoss
from .teach import TapTrajectoryRecorder, TeachStore, load_raw_frames, prepare_playback_frames
from .workzero import (
    WORK_ZERO_SCHEMA_VERSION,
    WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK,
    WorkZeroPose,
    WorkZeroStore,
    WorkZeroValidationError,
)
from .workzero_motion import ImmediateDoneMotion, WORKZERO_SMALL_RESIDUAL

SERVICE_PREFIX = "/panthera.arm.v1.ArmService/"

logger = logging.getLogger(__name__)
DEFAULT_FRICTION_FC = np.array([0.20, 0.15, 0.15, 0.15, 0.04, 0.04], dtype=np.float64)
DEFAULT_FRICTION_FV = np.array([0.06, 0.06, 0.06, 0.03, 0.02, 0.02], dtype=np.float64)

# 电机↔URDF 关节轴符号 / zero offset 与 URDF 零点的坐标契约尚未验证
# （见 docs/COORDINATE_CONTRACT.md）。真实 MIT teach/playback 依赖
# get_Gravity(q) 前馈，符号错误会直接产生非预期运动，故默认拒绝。
TEACH_CONTRACT_GATE_REASON = (
    "重力/反馈坐标契约未验证：默认拒绝真实 MIT teach/playback；"
    "请先完成 docs/COORDINATE_CONTRACT.md 的静态与真机核对，"
    "再以 --allow-unverified-teach / PANTHERA_ALLOW_UNVERIFIED_TEACH=1 显式放行"
)

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
    "ApplyPolicyChunk",
    "TeachStart",
    "TeachClutch",
    "TeachStop",
    "TeachRecordStart",
    "TeachRecordStop",
    "TeachPlay",
    "CancelExecution",
    "SetWorkZero",
    "GoWorkZero",
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
        policy_config: PolicySafetyConfig | None = None,
        policy_path_validator=None,
        policy_confirmations: PolicyConfirmationManager | None = None,
        policy_asset_allow_list: PolicyAssetAllowList | None = None,
        allow_unverified_teach: bool | None = None,
        teach_gravity_scale: float | np.ndarray = 1.0,
        teach_gravity_scale_high: float | np.ndarray | None = None,
        teach_gravity_breakpoint: float | np.ndarray | None = None,
        teach_gravity_segmented: bool = False,
        teach_gravity_residual: float | np.ndarray = 0.0,
        auto_hold_enabled: bool = True,
        teach_manual_clutch: bool = False,
        teach_safe_hold_s: float = 10.0,
        work_zero_store: WorkZeroStore | None = None,
        workzero_enabled: bool | None = None,
        workzero_real_hardware_enabled: bool | None = None,
    ) -> None:
        self._hardware_loop = hardware_loop
        self._leases = leases
        self._kinematics = kinematics
        self._executions = executions
        # None=自动（仿真放行/真机拒绝）；False=一律拒绝 MIT；True=一律放行 MIT。
        # POS-VEL 回放不使用重力前馈，始终不受本门控影响。
        self._allow_unverified_teach = allow_unverified_teach
        scale = np.asarray(teach_gravity_scale, dtype=np.float64)
        self._teach_gravity_scale = float(scale) if scale.shape == () else scale.copy()
        if teach_gravity_scale_high is not None:
            high = np.asarray(teach_gravity_scale_high, dtype=np.float64)
            self._teach_gravity_scale_high = float(high) if high.shape == () else high.copy()
        else:
            self._teach_gravity_scale_high = None
        if teach_gravity_breakpoint is not None:
            bp = np.asarray(teach_gravity_breakpoint, dtype=np.float64)
            self._teach_gravity_breakpoint = float(bp) if bp.shape == () else bp.copy()
        else:
            self._teach_gravity_breakpoint = None
        self._teach_gravity_segmented = bool(teach_gravity_segmented)
        residual = np.asarray(teach_gravity_residual, dtype=np.float64)
        self._teach_gravity_residual = float(residual) if residual.shape == () else residual.copy()
        self._auto_hold_enabled = bool(auto_hold_enabled)
        self._teach_manual_clutch = bool(teach_manual_clutch)
        self._teach_safe_hold_s = float(teach_safe_hold_s)
        self._work_zero_store = work_zero_store if work_zero_store is not None else WorkZeroStore()
        self._workzero_enabled = (
            os.environ.get("PANTHERA_WORKZERO_ENABLED", "0") == "1"
            if workzero_enabled is None
            else bool(workzero_enabled)
        )
        self._workzero_real_hardware_enabled = (
            os.environ.get("PANTHERA_WORKZERO_REAL_HARDWARE_ENABLED", "0") == "1"
            if workzero_real_hardware_enabled is None
            else bool(workzero_real_hardware_enabled)
        )
        # gozero 到位后的 teach-lock finalizer 任务集；close 时统一取消
        self._gozero_finalize_tasks: set[asyncio.Task[None]] = set()
        self._started_at = time.monotonic()
        self._unary_jog_motion: JointJogMotion | None = None
        self._unary_jog_completion = None
        self._unary_jog_token = ""
        self._teach_store = TeachStore()
        self._teach_motion: TeachMotion | None = None
        self._teach_completion = None
        self._teach_monitor_task: asyncio.Task[None] | None = None
        self._recorder: TapTrajectoryRecorder | None = None
        self._recorder_lock = asyncio.Lock()
        self._policy_config = policy_config or PolicySafetyConfig()
        self._policy_path_validator = policy_path_validator
        self._policy_confirmations = policy_confirmations or PolicyConfirmationManager()
        self._policy_asset_allow_list = policy_asset_allow_list
        self._policy_lock = asyncio.Lock()
        self._policy_session_id = ""
        self._policy_last_request_id = ""
        self._policy_last_observation_sequence = 0

    def _reset_policy_session(self) -> None:
        self._policy_session_id = ""
        self._policy_last_request_id = ""
        self._policy_last_observation_sequence = 0
        self._policy_confirmations.revoke_all()

    async def _teach_contract_gate(self, *, mit: bool) -> str:
        """坐标契约未验证时默认拒绝真实 MIT teach/playback；返回空串表示放行。"""
        if not mit:
            return ""
        if self._allow_unverified_teach is True:
            return ""
        if self._allow_unverified_teach is False:
            return TEACH_CONTRACT_GATE_REASON
        is_sim = await asyncio.wrap_future(
            self._hardware_loop.submit(lambda backend: backend.is_sim)
        )
        if is_sim:
            return ""
        return TEACH_CONTRACT_GATE_REASON

    async def AcquireControl(self, request, context):
        try:
            result = self._leases.acquire(request.client_id, force=request.force)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if result.replaced_holder:
            self._reset_policy_session()
            cancelled = await self._cancel_active_motion_and_wait(CancelReason.FORCE_ACQUIRE)
            if cancelled:
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
        self._reset_policy_session()
        cancelled = await self._cancel_active_motion_and_wait(CancelReason.CLIENT)
        if cancelled:
            await asyncio.wrap_future(
                self._hardware_loop.submit(lambda backend: backend.enter_idle_damping())
            )
        return arm_pb2.Empty()

    async def _cancel_active_motion_and_wait(
        self,
        reason: CancelReason,
        *,
        timeout_s: float = 0.5,
    ) -> bool:
        """释放控制权前，等待非阻塞运动完成安全减速，再进入被动空闲。"""
        if not self._hardware_loop.has_active_motion:
            return True
        self._hardware_loop.request_cancel(reason)
        deadline = time.monotonic() + timeout_s
        while self._hardware_loop.has_active_motion and time.monotonic() < deadline:
            await asyncio.sleep(min(self._hardware_loop.period_s, 0.01))
        if self._hardware_loop.has_active_motion:
            teach = self.arm_service._teach_motion
            if teach is not None and teach.safe_holding:
                # 显式离合 teach 取消后的安全保持自行限时结束；不升级 EStop。
                return True
            self._hardware_loop.request_estop()
            estop_deadline = time.monotonic() + 0.2
            while not self._hardware_loop.estop_applied and time.monotonic() < estop_deadline:
                await asyncio.sleep(min(self._hardware_loop.period_s, 0.005))
            return False
        return True

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
        self._reset_policy_session()
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
        deadline = time.monotonic() + 0.2
        while (
            not self._hardware_loop.estop_recovery_applied
            and not self._hardware_loop.estop_recovery_error
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(min(self._hardware_loop.period_s, 0.005))
        recovery_error = self._hardware_loop.estop_recovery_error
        if recovery_error:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"急停复位安全阻尼建立失败: {recovery_error}",
            )
        if not self._hardware_loop.estop_recovery_applied:
            self._hardware_loop.request_estop()
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "急停复位安全阻尼未在 200ms 内建立")
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

    @staticmethod
    def _work_zero_pose_message(pose: WorkZeroPose) -> arm_pb2.WorkZeroPose:
        message = arm_pb2.WorkZeroPose(
            schema_version=pose.schema_version,
            joints=pose.joints,
            gripper=pose.gripper,
            captured_at_ms=pose.captured_at_ms,
            stream_instance_id=pose.stream_instance_id,
            source=pose.source,
        )
        if pose.sampled_monotonic_ns is not None:
            message.sampled_monotonic_ns = pose.sampled_monotonic_ns
        if pose.state_sequence is not None:
            message.state_sequence = pose.state_sequence
        return message

    async def GetWorkZero(self, request, context):
        del request, context
        try:
            pose = await asyncio.to_thread(self._work_zero_store.load)
        except WorkZeroValidationError as exc:
            return arm_pb2.GetWorkZeroResponse(exists=False, reject_reason=str(exc))
        if pose is None:
            return arm_pb2.GetWorkZeroResponse(exists=False)
        response = arm_pb2.GetWorkZeroResponse(
            exists=True,
            pose=self._work_zero_pose_message(pose),
        )
        try:
            limits = await asyncio.wrap_future(
                self._hardware_loop.submit(lambda backend: backend.limits)
            )
            self._work_zero_store.validate_with_limits(pose, limits)
        except WorkZeroValidationError as exc:
            response.reject_reason = str(exc)
        return response

    async def SetWorkZero(self, request, context):
        if not request.confirm:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "SetWorkZero 必须 confirm=true")
        await self._refresh_teach_motion()
        motion = self._teach_motion
        if motion is None:
            return arm_pb2.SetWorkZeroResponse(
                accepted=False,
                reject_reason="当前没有运行中的 teach；请先启动显式离合示教（teach start --manual-clutch）",
            )
        if not motion.manual_clutch:
            return arm_pb2.SetWorkZeroResponse(
                accepted=False,
                reject_reason="当前 teach 未启用显式 clutch，不能保存工作零位",
            )
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        timeout_s = optional_double(request, "lock_wait_timeout_s", 2.0)
        if timeout_s <= 0 or not np.isfinite(timeout_s) or timeout_s > 10.0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "lock_wait_timeout_s 必须位于 (0, 10]")
        generation_before = motion.lock_generation
        motion.request_clutch(TeachClutchCommand.LOCK)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and motion.lock_generation <= generation_before:
            await asyncio.sleep(0.02)
        if motion.lock_generation <= generation_before:
            return arm_pb2.SetWorkZeroResponse(
                accepted=False,
                reject_reason="等待 lock generation 超时：teach 控制循环未消费 LOCK，请确认 teach 正在运行",
            )
        snapshot = motion.lock_snapshot
        if snapshot is None:
            return arm_pb2.SetWorkZeroResponse(
                accepted=False,
                reject_reason="lock snapshot 不可用",
            )
        cached = self._hardware_loop.latest_state()
        pose = WorkZeroPose(
            schema_version=WORK_ZERO_SCHEMA_VERSION,
            joints=snapshot.joints,
            gripper=snapshot.gripper,
            captured_at_ms=int(time.time() * 1000),
            sampled_monotonic_ns=snapshot.captured_monotonic_ns,
            state_sequence=None,
            stream_instance_id=cached.stream_instance_id if cached is not None else "",
            source=WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK,
        )
        try:
            limits = await asyncio.wrap_future(
                self._hardware_loop.submit(lambda backend: backend.limits)
            )
            await asyncio.to_thread(self._work_zero_store.save, pose, limits)
        except WorkZeroValidationError as exc:
            return arm_pb2.SetWorkZeroResponse(accepted=False, reject_reason=str(exc))
        except OSError as exc:
            return arm_pb2.SetWorkZeroResponse(
                accepted=False,
                reject_reason=f"工作零位文件保存失败: {exc}",
            )
        return arm_pb2.SetWorkZeroResponse(
            accepted=True,
            saved=True,
            pose=self._work_zero_pose_message(pose),
            lock_generation=snapshot.generation,
        )

    async def GoWorkZero(self, request, context):
        """回到工作零位：MoveL（真机已验证 POS-VEL 轨迹）到工作零位，
        到位后服务端自动切 teach manual-clutch LOCK 定住。

        真机验收修订（2026-08-16）：原 WorkZeroMotion 零前馈纯 kp/kd 伺服在
        承重高位形稳态误差（重力/kp）远超 settle 判据，真机无法收敛；改为
        复用 MoveL 轨迹（固件 POS-VEL 保位）+ teach HOLD（标定重力前馈 +
        MANUAL_CLUTCH_KP_HOLD）。rezero 调用同一 RPC（reason=post_action）。
        """
        if not request.confirm:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "GoWorkZero 必须 confirm=true")
        if request.reason not in ("", "gozero", "post_action"):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "reason 必须为空、gozero 或 post_action")
        if not self._workzero_enabled:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "work-zero 功能未启用（服务端 PANTHERA_WORKZERO_ENABLED=1 开启）",
            )
        # rezero（post_action）时 teach HOLD 是流程的一部分，允许其占用 active motion；
        # 其它任何运动都拒绝。
        if self._hardware_loop.has_active_motion and (
            request.reason != "post_action" or self._teach_motion is None
        ):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "已有运动正在执行")
        if self._recorder is not None:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "录制进行中，禁止回位")
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        try:
            pose = await asyncio.to_thread(self._work_zero_store.load)
        except WorkZeroValidationError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        if pose is None:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "尚未保存工作零位；请先 workzero setzero",
            )
        timeout_s = optional_double(request, "timeout_s", 30.0)
        if timeout_s <= 0 or not np.isfinite(timeout_s) or timeout_s > 60.0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "timeout_s 必须位于 (0, 60]")
        is_sim = await asyncio.wrap_future(
            self._hardware_loop.submit(lambda backend: backend.is_sim)
        )
        if not is_sim and not self._workzero_real_hardware_enabled:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "真机 WorkZeroMotion 未启用（服务端 PANTHERA_WORKZERO_REAL_HARDWARE_ENABLED=1 "
                "且完成 P6 分级验收后才会放行）",
            )
        limits = await asyncio.wrap_future(
            self._hardware_loop.submit(lambda backend: backend.limits)
        )
        try:
            self._work_zero_store.validate_with_limits(pose, limits)
        except WorkZeroValidationError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        cached = self._hardware_loop.latest_state()
        if cached is None or not all(state.valid for state in cached.motors[:6]):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "关节状态无效或连接不完整")
        current = np.array([state.position for state in cached.motors[:6]], dtype=np.float64)
        target_positions = np.asarray(pose.joints, dtype=np.float64)

        # ---- rezero：动作完成位（teach HOLD）→ 开爪（松方块，脚本做不是模型做）
        #      → 快速安全退出 teach → MoveL 回工作0位 → teach lock ----
        if request.reason == "post_action":
            await self._refresh_teach_motion()
            teach = self._teach_motion
            if teach is None or not teach.manual_clutch:
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "rezero 需要 teach HOLD 状态：动作完成后请先 lock 保持",
                )
            teach.request_gripper(pose.gripper)
            release_deadline = time.monotonic() + 4.0
            released = False
            while time.monotonic() < release_deadline:
                state = self._hardware_loop.latest_state()
                if state is not None and all(motor.valid for motor in state.motors):
                    if abs(state.motors[6].position - pose.gripper) <= 0.02:
                        released = True
                        break
                await asyncio.sleep(0.05)
            if not released:
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "夹爪未在 4s 内打开到工作零位姿态；拒绝回位（可能仍抓着物体）",
                )
            teach.request_cancel_quick(CancelReason.CLIENT)
            exit_deadline = time.monotonic() + 1.5
            while self._hardware_loop.has_active_motion and time.monotonic() < exit_deadline:
                await asyncio.sleep(0.02)
            if self._hardware_loop.has_active_motion:
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "teach 未在 1.5s 内退出，拒绝回位",
                )
            await self._refresh_teach_motion()
            current = np.array(
                [state.position for state in self._hardware_loop.latest_state().motors[:6]],
                dtype=np.float64,
            )

        # ---- 小残差（已在工作零位）：立即 DONE，直接进入 teach lock 阶段 ----
        if float(np.max(np.abs(target_positions - current))) <= WORKZERO_SMALL_RESIDUAL:
            motion = ImmediateDoneMotion()
        else:
            # ---- MoveL 路径：FK 工作零位 → 笛卡尔轨迹（真机已验证 POS-VEL）----
            await self._kinematics.warm()
            try:
                target_fk = await self._kinematics.call("fk", {"q": target_positions})
                current_fk = await self._kinematics.call("fk", {"q": current})
            except ValueError as exc:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
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
                            {
                                "position": np.asarray(target_fk["position"]),
                                "rotation": np.asarray(target_fk["rotation"]),
                            },
                        ],
                        "duration": None,
                        "use_spline": True,
                    },
                )
            except ValueError as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            if result["fraction"] < 0.999 or not result["positions"]:
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    f"工作零位笛卡尔路径仅完成 {result['fraction'] * 100:.1f}%",
                )
            motion = CartesianTrajectoryMotion(
                positions=[np.asarray(value) for value in result["positions"]],
                velocities=[np.asarray(value) for value in result["velocities"]],
                timestamps=list(result["timestamps"]),
                max_torque=limits.joint_torque,
                tolerance=0.01,  # 工作零位到位容差（0.57°）
                settle_timeout_s=min(4.0, timeout_s),
                operation_name="gozero-movel",
            )
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        execution_id = self._executions.register(motion, completion)
        finalizer = asyncio.create_task(
            self._gozero_finalize_teach(completion, token, pose),
            name=f"panthera-gozero-finalize-{execution_id[:8]}",
        )
        self._gozero_finalize_tasks.add(finalizer)
        finalizer.add_done_callback(self._gozero_finalize_tasks.discard)
        return arm_pb2.ExecutionAccepted(execution_id=execution_id)

    async def _gozero_finalize_teach(self, completion, token: str, pose: WorkZeroPose) -> None:
        """回位到位后：自动启动 teach manual-clutch 并 LOCK 定住。

        失败/取消/lease 失效时绝不进入 teach（与 WZ-4 一致：不自动恢复运动）。
        LOCK 前先让 teach 在重力补偿 DRAG 下稳定 0.3s，再锚定当前位形，
        避免 MoveL 末点到 HOLD 切换瞬态被锚定。
        """
        try:
            result = await asyncio.wrap_future(completion)
        except (asyncio.CancelledError, RuntimeError):
            return
        if result is not MotionStepResult.DONE:
            logger.warning("workzero 回位未完成（%s），不进入 teach HOLD", result.value)
            return
        if self._hardware_loop.has_active_motion:
            logger.warning("workzero 到位后仍有活动运动，跳过 teach HOLD")
            return
        if self._teach_motion is not None:
            logger.info("teach 已存在，跳过重复启动")
            return
        if not self._leases.heartbeat(token):
            logger.warning("workzero 到位后 lease 失效，不启动 teach HOLD")
            return
        gate_reason = await self._teach_contract_gate(mit=True)
        if gate_reason:
            logger.warning("teach 契约门拒绝：%s", gate_reason)
            return
        try:
            motion = TeachMotion(
                kp=np.zeros(6),
                kd=np.zeros(6),
                fc=DEFAULT_FRICTION_FC.copy(),
                fv=DEFAULT_FRICTION_FV.copy(),
                gravity_scale=self._teach_gravity_scale,
                gravity_scale_high=self._teach_gravity_scale_high,
                gravity_breakpoint=self._teach_gravity_breakpoint,
                gravity_segmented=self._teach_gravity_segmented,
                gravity_residual=self._teach_gravity_residual,
                auto_hold=(
                    AutoHoldConfig() if self._auto_hold_enabled else AutoHoldConfig(enabled=False)
                ),
                manual_clutch=True,
                safe_hold_time_s=self._teach_safe_hold_s,
            )
        except ValueError as exc:
            logger.warning("teach 构造失败：%s", exc)
            return
        accepted, teach_completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            logger.warning("teach 启动失败：%s", exc)
            return
        self._teach_motion = motion
        self._teach_completion = teach_completion
        self._teach_monitor_task = asyncio.create_task(
            self._monitor_teach_completion(teach_completion),
            name="panthera-teach-monitor",
        )
        # 先让重力补偿 DRAG 稳定，再 LOCK 锚定（避免切换瞬态被锁存）
        await asyncio.sleep(0.3)
        if not self._leases.heartbeat(token):
            logger.warning("workzero teach 稳定期间 lease 失效，保持 DRAG 不 LOCK")
            return
        motion.request_clutch(TeachClutchCommand.LOCK)
        logger.info("workzero 已到位并进入 teach HOLD")
        # 开爪到工作零位夹爪姿态（HOLD 帧内受限 MIT 阻抗，臂保持不动）
        motion.request_gripper(pose.gripper)
        release_deadline = time.monotonic() + 4.0
        while time.monotonic() < release_deadline:
            state = self._hardware_loop.latest_state()
            if state is not None and all(motor.valid for motor in state.motors):
                if abs(state.motors[6].position - pose.gripper) <= 0.02:
                    logger.info("workzero 夹爪已打开到工作零位姿态")
                    return
            await asyncio.sleep(0.05)
        logger.warning("workzero 夹爪未在 4s 内到达工作零位姿态，保持 HOLD 待人工处理")

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

    async def StreamMeasuredState(
        self,
        request,
        context,
    ) -> AsyncIterator[arm_pb2.MeasuredStateSample]:
        tap = self._hardware_loop.state_tap
        after_sequence = int(request.after_sequence)
        if request.start_at_latest:
            after_sequence = tap.stats().newest_sequence
        try:
            while True:
                try:
                    cached = await asyncio.to_thread(
                        tap.read_after,
                        after_sequence,
                        0.5,
                    )
                except StateTapDataLoss as exc:
                    await context.abort(grpc.StatusCode.DATA_LOSS, str(exc))
                if cached is None:
                    if tap.stats().closed:
                        return
                    continue
                stats = tap.stats()
                yield arm_pb2.MeasuredStateSample(
                    state=robot_state_message(
                        cached,
                        estop_engaged=self._hardware_loop.estop_engaged,
                    ),
                    stream_instance_id=cached.stream_instance_id,
                    oldest_available_sequence=stats.oldest_sequence,
                    overwritten_samples_total=stats.overwritten_samples_total,
                )
                after_sequence = cached.sequence
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
        velocities = (positions - current) / request.duration_s
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

    async def CartesianJog(
        self,
        request_iterator,
        context,
    ) -> AsyncIterator[arm_pb2.CartesianJogFeedback]:
        iterator = request_iterator.__aiter__()
        try:
            first = await iterator.__anext__()
        except StopAsyncIteration:
            return
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))

        async def command_values(command) -> tuple[np.ndarray, float]:
            linear = finite_vector(command.linear_velocity, name="linear_velocity", length=3)
            angular = finite_vector(command.angular_velocity, name="angular_velocity", length=3)
            damping = optional_double(command, "damping", 0.01)
            if damping < 0 or not np.isfinite(damping):
                raise ValueError("damping 必须是非负有限数值")
            cached = self._hardware_loop.latest_state()
            if cached is None or not all(state.valid for state in cached.motors[:6]):
                raise ValueError("关节状态无效或连接不完整")
            q = np.asarray([state.position for state in cached.motors[:6]], dtype=np.float64)
            result = await self._kinematics.call(
                "cartesian_jog",
                {
                    "q": q,
                    "twist": np.concatenate((linear, angular)),
                    "damping": damping,
                },
            )
            velocity = np.asarray(result["joint_velocity"], dtype=np.float64)
            reason = arm_magnitude_reject_reason(velocity, limits.joint_velocity, label="关节速度")
            if reason:
                raise ValueError(reason)
            return velocity, float(result["manipulability"])

        try:
            first_velocity, manipulability = await command_values(first)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        motion = JointJogMotion(freshness_s=0.12)
        motion.update(first_velocity)
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

        async def consume_commands() -> None:
            nonlocal manipulability
            async for command in iterator:
                velocity, current_mu = await command_values(command)
                if not self._leases.heartbeat(token):
                    raise PermissionError("控制权 lease 已失效")
                manipulability = current_mu
                motion.update(velocity)

        consumer = asyncio.create_task(consume_commands(), name="panthera-cartesian-jog-consumer")
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
                    yield arm_pb2.CartesianJogFeedback(
                        joint_state=joint_state_message(cached),
                        manipulability=manipulability,
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

    async def RunJointTrajectory(self, request, context):
        if self._hardware_loop.has_active_motion:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "已有运动正在执行")
        if len(request.waypoints) < 2:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "至少需要 2 个 waypoint")
        if len(request.durations) != len(request.waypoints) - 1:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "durations 数量必须比 waypoints 少 1")
        try:
            positions = [
                finite_vector(waypoint.positions, name=f"waypoints[{index}].positions")
                for index, waypoint in enumerate(request.waypoints)
            ]
            velocities = [
                (
                    finite_vector(waypoint.velocities, name=f"waypoints[{index}].velocities")
                    if waypoint.velocities
                    else None
                )
                for index, waypoint in enumerate(request.waypoints)
            ]
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        durations = np.asarray(request.durations, dtype=np.float64)
        if not np.all(np.isfinite(durations)) or np.any(durations <= 0):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "durations 必须全部为正有限数值")
        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        for values in positions:
            reason = arm_position_reject_reason(values, limits)
            if reason:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, reason)
        for values in velocities:
            if values is None:
                continue
            reason = arm_magnitude_reject_reason(values, limits.joint_velocity, label="边界速度")
            if reason:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, reason)
        try:
            result = await self._kinematics.call(
                "joint_trajectory",
                {
                    "waypoints": positions,
                    "velocities": velocities,
                    "durations": durations.tolist(),
                },
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        motion = CartesianTrajectoryMotion(
            positions=[np.asarray(value) for value in result["positions"]],
            velocities=[np.asarray(value) for value in result["velocities"]],
            timestamps=list(result["timestamps"]),
            max_torque=limits.joint_torque,
            operation_name="trajectory",
        )
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        execution_id = self._executions.register(motion, completion)
        return arm_pb2.ExecutionAccepted(execution_id=execution_id)

    async def ApplyPolicyChunk(self, request, context):
        async with self._policy_lock:
            if self._hardware_loop.has_active_motion:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="已有运动正在执行",
                )
            if self._policy_session_id and request.session_id != self._policy_session_id:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="policy session_id 不匹配",
                )
            if request.request_id == self._policy_last_request_id:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="policy request_id 重复",
                )
            if request.observation_sequence <= self._policy_last_observation_sequence:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="policy observation_sequence 未递增",
                )
            if request.server_elapsed_ns < 0:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="server_elapsed_ns 不得为负数",
                )
            cached = self._hardware_loop.latest_state()
            if cached is None:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="尚无电机状态缓存",
                )
            limits, is_sim = await asyncio.wrap_future(
                self._hardware_loop.submit(lambda backend: (backend.limits, backend.is_sim))
            )
            if not is_sim and self._policy_path_validator is None:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="真机 policy swept-path validator 尚未配置",
                )
            if not is_sim and self._policy_asset_allow_list is None:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="真机 policy asset allow-list 尚未配置",
                )
            if not is_sim and not self._policy_asset_allow_list.allows(
                checkpoint_sha256=request.checkpoint_sha256,
                stats_sha256=request.stats_sha256,
                schema_sha256=request.schema_sha256,
            ):
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="policy checkpoint/stats/schema identity 不在部署 allow-list",
                )
            if not is_sim and not self._policy_confirmations.consume(
                request.operator_confirmation_id,
                request_id=request.request_id,
                session_id=request.session_id,
            ):
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason="真机 policy confirmation 无效、过期、已使用或与 request/session 不匹配",
                )
            try:
                chunk = validate_policy_chunk(
                    request_id=request.request_id,
                    session_id=request.session_id,
                    observation_sequence=int(request.observation_sequence),
                    observation_sampled_monotonic_ns=int(request.observation_sampled_monotonic_ns),
                    state_stream_instance_id=request.state_stream_instance_id,
                    deadline_pi_monotonic_ns=int(request.deadline_pi_monotonic_ns),
                    waypoint_positions=[waypoint.positions for waypoint in request.waypoints],
                    step_offsets_ns=[waypoint.step_offset_ns for waypoint in request.waypoints],
                    checkpoint_sha256=request.checkpoint_sha256,
                    stats_sha256=request.stats_sha256,
                    schema_sha256=request.schema_sha256,
                    cached_state=cached,
                    observation_state=self._hardware_loop.state_tap.get(int(request.observation_sequence)),
                    limits=limits,
                    now_monotonic_ns=time.monotonic_ns(),
                    config=self._policy_config,
                    path_validator=self._policy_path_validator,
                )
            except PolicyValidationError as exc:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason=str(exc),
                )
            token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
            if not self._leases.heartbeat(token):
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
            motion = PolicyChunkMotion(
                chunk,
                forward_kinematics=(
                    self._policy_path_validator.tool_position
                    if not is_sim and self._policy_path_validator is not None
                    else None
                ),
            )
            accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
            try:
                await asyncio.wrap_future(accepted)
            except RuntimeError as exc:
                return arm_pb2.ApplyPolicyChunkResponse(
                    accepted=False,
                    reject_reason=str(exc),
                )
            self._policy_session_id = request.session_id
            self._policy_last_request_id = request.request_id
            self._policy_last_observation_sequence = int(request.observation_sequence)
            execution_id = self._executions.register(motion, completion)
            metrics = chunk.metrics
            return arm_pb2.ApplyPolicyChunkResponse(
                accepted=True,
                execution_id=execution_id,
                max_joint_delta=metrics.max_joint_delta,
                max_gripper_delta=metrics.max_gripper_delta,
                sampled_max_velocity=metrics.sampled_max_velocity,
                sampled_max_acceleration=metrics.sampled_max_acceleration,
                sampled_max_jerk=metrics.sampled_max_jerk,
            )

    async def GetPolicyAcceptance(self, request, context):
        del context
        record = self._executions.record(request.execution_id)
        if record is None or not isinstance(record.motion, PolicyChunkMotion):
            return arm_pb2.PolicyAcceptanceResponse(
                terminal=False,
                passed=False,
                tolerance_m=self._policy_config.hardware_endpoint_tolerance_m,
                reject_reason="unknown policy execution_id",
            )
        snapshot = self._executions.snapshot(request.execution_id)
        if snapshot is None or not snapshot.terminal:
            return arm_pb2.PolicyAcceptanceResponse(
                terminal=False,
                passed=False,
                tolerance_m=self._policy_config.hardware_endpoint_tolerance_m,
            )
        endpoint_error = record.motion.endpoint_error_m
        passed = snapshot.result is MotionStepResult.DONE and (
            endpoint_error is None or endpoint_error <= self._policy_config.hardware_endpoint_tolerance_m
        )
        return arm_pb2.PolicyAcceptanceResponse(
            terminal=True,
            passed=passed,
            endpoint_error_m=endpoint_error or 0.0,
            tolerance_m=self._policy_config.hardware_endpoint_tolerance_m,
            reject_reason=snapshot.error_message,
        )

    async def TeachStart(self, request, context):
        await self._refresh_teach_motion()
        if self._hardware_loop.has_active_motion:
            return arm_pb2.TeachStartResponse(
                accepted=False,
                reject_reason="已有运动正在执行",
            )
        gate_reason = await self._teach_contract_gate(mit=True)
        if gate_reason:
            return arm_pb2.TeachStartResponse(accepted=False, reject_reason=gate_reason)
        try:
            kp = finite_vector(request.kp, name="kp") if request.kp else np.zeros(6)
            kd = finite_vector(request.kd, name="kd") if request.kd else np.zeros(6)
            fc = finite_vector(request.fc, name="fc") if request.fc else DEFAULT_FRICTION_FC.copy()
            fv = finite_vector(request.fv, name="fv") if request.fv else DEFAULT_FRICTION_FV.copy()
            motion = TeachMotion(
                kp=kp,
                kd=kd,
                fc=fc,
                fv=fv,
                gravity_scale=self._teach_gravity_scale,
                gravity_scale_high=self._teach_gravity_scale_high,
                gravity_breakpoint=self._teach_gravity_breakpoint,
                gravity_segmented=self._teach_gravity_segmented,
                gravity_residual=self._teach_gravity_residual,
                auto_hold=AutoHoldConfig() if self._auto_hold_enabled else AutoHoldConfig(enabled=False),
                manual_clutch=bool(request.manual_clutch) or self._teach_manual_clutch,
                safe_hold_time_s=self._teach_safe_hold_s,
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            return arm_pb2.TeachStartResponse(accepted=False, reject_reason=str(exc))
        self._teach_motion = motion
        self._teach_completion = completion
        self._teach_monitor_task = asyncio.create_task(
            self._monitor_teach_completion(completion),
            name="panthera-teach-monitor",
        )
        return arm_pb2.TeachStartResponse(accepted=True)

    async def TeachClutch(self, request, context):
        await self._refresh_teach_motion()
        motion = self._teach_motion
        if motion is None:
            return arm_pb2.TeachClutchResponse(
                accepted=False,
                reject_reason="当前没有运行中的 teach",
            )
        if not motion.manual_clutch:
            return arm_pb2.TeachClutchResponse(
                accepted=False,
                reject_reason="当前 teach 未启用显式 clutch；请用 teach start --manual-clutch",
                state=motion.auto_hold_state.value,
            )
        commands = {
            arm_pb2.TEACH_CLUTCH_MODE_LOCK: TeachClutchCommand.LOCK,
            arm_pb2.TEACH_CLUTCH_MODE_DRAG: TeachClutchCommand.DRAG,
        }
        command = commands.get(request.mode)
        if command is None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "mode 必须为 lock 或 drag")
        try:
            motion.request_clutch(command)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        return arm_pb2.TeachClutchResponse(
            accepted=True,
            state=motion.auto_hold_state.value,
        )

    async def TeachStop(self, request, context):
        del request
        await self._refresh_teach_motion()
        motion = self._teach_motion
        completion = self._teach_completion
        monitor = self._teach_monitor_task
        if motion is None or completion is None:
            return arm_pb2.TeachStopResponse(accepted=False)
        motion.request_cancel(CancelReason.CLIENT)
        recorder_error = None
        try:
            await self._stop_recorder()
        except (OSError, RuntimeError, TimeoutError) as exc:
            recorder_error = exc
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(completion)),
                timeout=0.5,
            )
        except (TimeoutError, asyncio.TimeoutError, RuntimeError):
            pass
        # 显式离合 teach 取消后进入有限时长的 SAFE_HOLD：运动尚未结束。
        # 此时保留引用，由 monitor 任务在 SAFE_HOLD 结束后统一清理，
        # 否则 watchdog/ReleaseControl 会失去 safe_holding 判断依据。
        if self._teach_completion is completion and completion.done():
            self._teach_motion = None
            self._teach_completion = None
        if self._teach_monitor_task is monitor and completion.done():
            self._teach_monitor_task = None
        if monitor is not None and monitor is not asyncio.current_task() and completion.done():
            monitor.cancel()
            try:
                await monitor
            except (asyncio.CancelledError, RuntimeError):
                pass
        if recorder_error is not None:
            await context.abort(grpc.StatusCode.INTERNAL, str(recorder_error))
        return arm_pb2.TeachStopResponse(accepted=True)

    async def TeachRecordStart(self, request, context):
        await self._refresh_teach_motion()
        if self._recorder is not None:
            return arm_pb2.TeachRecordStartResponse(
                accepted=False,
                path=str(self._recorder.path),
            )
        if self._teach_motion is None:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "请先启动拖动示教")
        flush_interval = optional_double(request, "flush_interval", 0.2)
        try:
            path = self._teach_store.recording_path(request.path)
            recorder = TapTrajectoryRecorder(
                path,
                self._hardware_loop.state_tap,
                flush_interval=flush_interval,
            )
        except (OSError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        self._recorder = recorder
        return arm_pb2.TeachRecordStartResponse(accepted=True, path=str(path))

    async def TeachRecordStop(self, request, context):
        del request
        try:
            result = await self._stop_recorder()
        except (OSError, RuntimeError, TimeoutError) as exc:
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
        if result is None:
            return arm_pb2.TeachRecordStopResponse(accepted=False)
        path, frame_count = result
        return arm_pb2.TeachRecordStopResponse(
            accepted=True,
            saved_path=path,
            frame_count=frame_count,
        )

    async def TeachPlay(self, request, context):
        if self._hardware_loop.has_active_motion:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "已有运动正在执行")
        mode = "posvel" if request.mode == arm_pb2.PLAYBACK_MODE_POSVEL else "mit"
        gate_reason = await self._teach_contract_gate(mit=mode == "mit")
        if gate_reason:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, gate_reason)
        try:
            path = self._teach_store.existing_path(request.path)
            frames = await asyncio.to_thread(load_raw_frames, path)
            prepared = await asyncio.to_thread(
                prepare_playback_frames,
                frames,
                playback_dt=optional_double(request, "playback_dt", 0.01),
                smooth_window=(request.smooth_window if request.HasField("smooth_window") else 7),
            )
            if mode == "mit" and (not request.kp or not request.kd):
                raise ValueError("MIT 回放必须提供 kp 和 kd")
            kp = finite_vector(request.kp, name="kp") if request.kp else np.zeros(6)
            kd = finite_vector(request.kd, name="kd") if request.kd else np.zeros(6)
            fc = finite_vector(request.fc, name="fc") if request.fc else DEFAULT_FRICTION_FC.copy()
            fv = finite_vector(request.fv, name="fv") if request.fv else DEFAULT_FRICTION_FV.copy()
            tau_limit = (
                finite_vector(request.tau_limit, name="tau_limit")
                if request.tau_limit
                else TEACH_TAU_LIMIT.copy()
            )
            vel_threshold = optional_double(request, "vel_threshold", 0.0)
            gripper_kp = optional_double(request, "gripper_kp", 5.0)
            gripper_kd = optional_double(request, "gripper_kd", 0.5)
        except FileNotFoundError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except (OSError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        limits = await asyncio.wrap_future(self._hardware_loop.submit(lambda backend: backend.limits))
        reason = arm_magnitude_reject_reason(tau_limit, limits.joint_torque, label="tau_limit")
        if reason or np.any(tau_limit <= 0):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                reason or "tau_limit 必须全部为正数",
            )
        for index, frame in enumerate(prepared):
            reason = arm_position_reject_reason(frame.position, limits)
            reason = reason or arm_magnitude_reject_reason(
                frame.velocity,
                limits.joint_velocity,
                label=f"frames[{index}] 速度",
            )
            if reason:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, reason)
            if frame.gripper_position is not None and not (
                limits.gripper_lower <= frame.gripper_position <= limits.gripper_upper
            ):
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"frames[{index}] 夹爪位置超过软限位",
                )
        token = metadata_value(context.invocation_metadata(), LEASE_METADATA_KEY)
        if not self._leases.heartbeat(token):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "控制权 lease 已失效")
        try:
            motion = TeachPlaybackMotion(
                frames=prepared,
                mode=mode,
                kp=kp,
                kd=kd,
                fc=fc,
                fv=fv,
                vel_threshold=vel_threshold,
                tau_limit=tau_limit,
                gripper_kp=gripper_kp,
                gripper_kd=gripper_kd,
                gravity_scale=self._teach_gravity_scale,
                gravity_scale_high=self._teach_gravity_scale_high,
                gravity_breakpoint=self._teach_gravity_breakpoint,
                gravity_segmented=self._teach_gravity_segmented,
                gravity_residual=self._teach_gravity_residual,
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        accepted, completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(accepted)
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        execution_id = self._executions.register(motion, completion)
        return arm_pb2.ExecutionAccepted(execution_id=execution_id)

    async def TeachList(self, request, context):
        del request, context
        response = arm_pb2.TeachListResponse()
        for item in await asyncio.to_thread(self._teach_store.list_files):
            response.files.add(
                path=str(item.path),
                recorded_at=item.recorded_at_ms,
                duration_s=item.duration_s,
                frame_count=item.frame_count,
            )
        return response

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

    async def close(self) -> None:
        try:
            await self._stop_recorder()
        except (OSError, RuntimeError, TimeoutError):
            pass
        for finalizer in list(self._gozero_finalize_tasks):
            finalizer.cancel()
            try:
                await finalizer
            except (asyncio.CancelledError, RuntimeError):
                pass
        self._gozero_finalize_tasks.clear()
        await self._refresh_teach_motion()
        if self._teach_motion is not None and self._teach_completion is not None:
            self._teach_motion.request_cancel(CancelReason.SHUTDOWN)
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(self._teach_completion)),
                    timeout=0.5,
                )
            except (TimeoutError, asyncio.TimeoutError, RuntimeError):
                pass
        self._teach_motion = None
        self._teach_completion = None
        monitor = self._teach_monitor_task
        self._teach_monitor_task = None
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()
            try:
                await monitor
            except (asyncio.CancelledError, RuntimeError):
                pass

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

    async def _refresh_teach_motion(self) -> None:
        completion = self._teach_completion
        if completion is None or not completion.done():
            return
        try:
            await self._stop_recorder()
        except (OSError, RuntimeError, TimeoutError):
            pass
        self._teach_motion = None
        self._teach_completion = None
        monitor = self._teach_monitor_task
        if monitor is not None and monitor is not asyncio.current_task():
            try:
                await monitor
            except (asyncio.CancelledError, RuntimeError):
                pass

    async def _monitor_teach_completion(self, completion) -> None:
        try:
            while not completion.done():
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return
        finally:
            if self._teach_completion is completion:
                try:
                    await self._stop_recorder()
                except (OSError, RuntimeError, TimeoutError):
                    pass
                self._teach_motion = None
                self._teach_completion = None
            if self._teach_monitor_task is asyncio.current_task():
                self._teach_monitor_task = None

    async def _stop_recorder(self) -> tuple[str, int] | None:
        async with self._recorder_lock:
            recorder = self._recorder
            if recorder is None:
                return None
            self._recorder = None
            frame_count = await asyncio.to_thread(recorder.close)
            return str(recorder.path), frame_count

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

        def validate(backend):
            limits = backend.limits
            if position < limits.gripper_lower:
                return False, f"gripper 目标 {position:.6g} 超过下限 {limits.gripper_lower:.6g}"
            if position > limits.gripper_upper:
                return False, f"gripper 目标 {position:.6g} 超过上限 {limits.gripper_upper:.6g}"
            if velocity > limits.gripper_velocity:
                return False, f"gripper 速度 {velocity:.6g} 超过限值 {limits.gripper_velocity:.6g}"
            if max_torque > limits.gripper_torque:
                return False, f"gripper 最大力矩 {max_torque:.6g} 超过限值 {limits.gripper_torque:.6g}"
            return True, ""

        try:
            accepted, reject_reason = await asyncio.wrap_future(self._hardware_loop.submit(validate))
        except (BackendError, LimitViolationError, ValueError) as exc:
            return arm_pb2.GripperMoveResponse(accepted=False, reject_reason=str(exc))
        if not accepted:
            return arm_pb2.GripperMoveResponse(accepted=False, reject_reason=reject_reason)

        motion = GripperPositionMotion(
            position=position,
            velocity=velocity,
            max_torque=max_torque,
        )
        start_ack, _completion = self._hardware_loop.start_motion_with_ack(motion)
        try:
            await asyncio.wrap_future(start_ack)
        except RuntimeError as exc:
            return arm_pb2.GripperMoveResponse(accepted=False, reject_reason=str(exc))
        return arm_pb2.GripperMoveResponse(accepted=True)

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
