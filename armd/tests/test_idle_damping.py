from __future__ import annotations

import numpy as np
import pytest

from armd.backend import IDLE_DAMPING_KD, PASSIVE_IDLE_KD, FrameMode, SimBackend
from armd.motion import position_frame


def test_enter_idle_damping_replaces_previous_position_lock() -> None:
    backend = SimBackend()
    backend.write_frame(
        position_frame(
            backend,
            arm_position=np.zeros(6),
            arm_velocity=np.full(6, 0.1),
            gripper_position=0.0,
        )
    )
    assert {state.mode for state in backend.read_all()} == {int(FrameMode.POS_VEL_TQE)}

    backend.enter_idle_damping()
    backend.maintain_idle()

    assert {state.mode for state in backend.read_all()} == {int(FrameMode.POS_VEL_TQE_KP_KD)}
    assert backend._last_frame is not None
    assert backend._last_frame.arm_kd == pytest.approx(IDLE_DAMPING_KD)


def test_default_and_released_idle_are_zero_damping() -> None:
    backend = SimBackend()

    backend.maintain_idle()
    assert backend._last_frame is not None
    assert backend._last_frame.arm_torque == pytest.approx([0.0] * 6)
    assert backend._last_frame.arm_kp == pytest.approx([0.0] * 6)
    assert backend._last_frame.arm_kd == pytest.approx(PASSIVE_IDLE_KD)
    assert backend._last_frame.gripper_kd == 0.0

    backend.write_frame(
        position_frame(
            backend,
            arm_position=np.full(6, 0.1),
            arm_velocity=np.full(6, 0.1),
            gripper_position=0.0,
        )
    )
    backend.enter_passive_idle()
    backend.maintain_idle()

    assert backend._last_frame is not None
    assert backend._last_frame.arm_kd == pytest.approx(PASSIVE_IDLE_KD)
