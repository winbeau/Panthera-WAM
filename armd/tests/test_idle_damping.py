from __future__ import annotations

import numpy as np
import pytest

from armd.backend import (
    PASSIVE_IDLE_KD,
    SMOOTH_IDLE_FILTER_TAU_S,
    FrameMode,
    SimBackend,
    filter_idle_velocity,
    smooth_idle_damping_frame,
)
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
    assert backend._last_frame.arm_torque == pytest.approx([0.0] * 6)
    assert backend._last_frame.arm_kp == pytest.approx([0.0] * 6)
    assert backend._last_frame.arm_kd == pytest.approx([0.0] * 6)


def test_explicit_passive_idle_remains_zero_damping() -> None:
    backend = SimBackend()

    backend.enter_passive_idle()
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


def test_filtered_software_damping_is_continuous_and_uses_zero_firmware_kd() -> None:
    measured = np.array([0.1, -0.1, 0.02, -0.02, 0.01, -0.01])
    first = filter_idle_velocity(
        np.zeros(6),
        measured,
        dt_s=0.005,
        tau_s=SMOOTH_IDLE_FILTER_TAU_S,
    )
    second = filter_idle_velocity(
        first,
        measured,
        dt_s=0.005,
        tau_s=SMOOTH_IDLE_FILTER_TAU_S,
    )

    assert np.all(np.abs(first) > 0)
    assert np.all(np.abs(first) < np.abs(second))
    assert np.all(np.abs(second) < np.abs(measured))

    backend = SimBackend()
    frame = smooth_idle_damping_frame(backend.limits, np.zeros(6), second, 0.0)
    assert frame.arm_kp == pytest.approx([0.0] * 6)
    assert frame.arm_kd == pytest.approx([0.0] * 6)
    assert np.all(np.sign(frame.arm_torque) == -np.sign(measured))
