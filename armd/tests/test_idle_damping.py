from __future__ import annotations

import numpy as np

from armd.backend import FrameMode, SimBackend
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
