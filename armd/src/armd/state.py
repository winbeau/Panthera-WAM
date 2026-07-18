"""HardwareLoop 状态缓存到 protobuf 的无副作用映射。"""

from __future__ import annotations

import time

from panthera_arm import arm_pb2

from .backend import MotorSnapshot
from .hardware_loop import CachedRobotState


def motor_state_message(snapshot: MotorSnapshot) -> arm_pb2.MotorState:
    valid = snapshot.valid
    return arm_pb2.MotorState(
        name=snapshot.name,
        motor_id=snapshot.motor_id,
        position=snapshot.position if valid else 0.0,
        velocity=snapshot.velocity if valid else 0.0,
        torque=snapshot.torque if valid else 0.0,
        motor_time=snapshot.motor_time if valid else 0.0,
        mode=snapshot.mode,
        fault=snapshot.fault,
        pos_limit_flag=snapshot.pos_limit_flag,
        tor_limit_flag=snapshot.tor_limit_flag,
        valid=valid,
    )


def joint_state_message(
    cached: CachedRobotState,
    *,
    timestamp_ms: int | None = None,
) -> arm_pb2.JointState:
    timestamp = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    return arm_pb2.JointState(
        joints=[motor_state_message(snapshot) for snapshot in cached.motors[:6]],
        timestamp_ms=timestamp,
    )


def gripper_state_message(
    cached: CachedRobotState,
    *,
    timestamp_ms: int | None = None,
) -> arm_pb2.GripperState:
    timestamp = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    return arm_pb2.GripperState(
        state=motor_state_message(cached.motors[6]),
        timestamp_ms=timestamp,
    )


def robot_state_message(
    cached: CachedRobotState,
    *,
    estop_engaged: bool,
    include_joints: bool = True,
    include_gripper: bool = True,
    now: float | None = None,
) -> arm_pb2.RobotState:
    timestamp_ms = int(time.time() * 1000)
    response = arm_pb2.RobotState(
        age_ms=round(cached.age_s(time.monotonic() if now is None else now) * 1000),
        estop_engaged=estop_engaged,
    )
    if include_joints:
        response.joint.CopyFrom(joint_state_message(cached, timestamp_ms=timestamp_ms))
    if include_gripper:
        response.gripper.CopyFrom(gripper_state_message(cached, timestamp_ms=timestamp_ms))
    return response
