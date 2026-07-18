"""按当前控制模式执行 watchdog 停止。"""

from __future__ import annotations

import enum

import numpy as np

from .backend import Backend, FrameMode, JointFrame


class WatchdogStopAction(str, enum.Enum):
    HOLD_POSITION = "hold_position"
    ZERO_VELOCITY = "zero_velocity"
    HOLD_MIT = "hold_mit"
    HARD_STOP = "hard_stop"
    ALREADY_STOPPED = "already_stopped"


def apply_watchdog_stop(backend: Backend) -> WatchdogStopAction:
    states = backend.read_all()
    if len(states) != 7 or not all(state.valid for state in states):
        backend.stop()
        return WatchdogStopAction.HARD_STOP

    arm_position = np.array([state.position for state in states[:6]])
    gripper_position = states[6].position
    modes = {state.mode for state in states}
    if len(modes) != 1:
        backend.stop()
        return WatchdogStopAction.HARD_STOP

    mode_value = modes.pop()
    try:
        mode = FrameMode(mode_value)
    except ValueError:
        backend.stop()
        return WatchdogStopAction.HARD_STOP

    if mode is FrameMode.POS_VEL_TQE:
        backend.write_frame(
            JointFrame(
                mode=FrameMode.POS_VEL_TQE,
                arm_position=arm_position,
                arm_velocity=np.full(6, 0.1),
                arm_max_torque=backend.limits.joint_torque,
                gripper_position=gripper_position,
                gripper_velocity=0.1,
                gripper_max_torque=backend.limits.gripper_torque,
            )
        )
        return WatchdogStopAction.HOLD_POSITION

    if mode is FrameMode.VELOCITY:
        backend.write_frame(
            JointFrame(
                mode=FrameMode.VELOCITY,
                arm_position=arm_position,
                arm_velocity=np.zeros(6),
                gripper_position=gripper_position,
                gripper_velocity=0.0,
            )
        )
        return WatchdogStopAction.ZERO_VELOCITY

    if mode is FrameMode.POS_VEL_TQE_KP_KD:
        backend.write_frame(
            JointFrame(
                mode=FrameMode.POS_VEL_TQE_KP_KD,
                arm_position=arm_position,
                arm_velocity=np.zeros(6),
                arm_torque=np.zeros(6),
                arm_kp=np.array([30.0, 50.0, 60.0, 25.0, 15.0, 10.0]),
                arm_kd=np.array([3.0, 5.0, 6.0, 2.5, 1.5, 1.0]),
                gripper_position=gripper_position,
                gripper_velocity=0.0,
                gripper_torque=0.0,
            )
        )
        return WatchdogStopAction.HOLD_MIT

    if mode in {FrameMode.STOP, FrameMode.BRAKE}:
        return WatchdogStopAction.ALREADY_STOPPED

    backend.stop()
    return WatchdogStopAction.HARD_STOP
