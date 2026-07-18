"""按当前控制模式执行 watchdog 停止。"""

from __future__ import annotations

import enum

from .backend import Backend, FrameMode


class WatchdogStopAction(str, enum.Enum):
    IDLE_DAMPING = "idle_damping"
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

    if mode in {FrameMode.POS_VEL_TQE, FrameMode.VELOCITY, FrameMode.POS_VEL_TQE_KP_KD}:
        backend.enter_idle_damping()
        backend.maintain_idle()
        return WatchdogStopAction.IDLE_DAMPING

    if mode in {FrameMode.STOP, FrameMode.BRAKE}:
        return WatchdogStopAction.ALREADY_STOPPED

    backend.stop()
    return WatchdogStopAction.HARD_STOP
