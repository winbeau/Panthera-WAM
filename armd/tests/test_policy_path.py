from __future__ import annotations

import numpy as np
import pytest

from armd.policy_path import ConservativePolicyPathValidator


def validator_for(point):
    return ConservativePolicyPathValidator(
        forward_kinematics=lambda _: point,
        table_z_min=0.05,
        base_radius_m=0.1,
        camera_boxes=(((0.2, 0.2, 0.2), (0.3, 0.3, 0.3)),),
    )


def test_hardware_policy_path_requires_calibrated_camera_box():
    with pytest.raises(ValueError, match="requires at least one"):
        ConservativePolicyPathValidator(
            forward_kinematics=lambda _: np.array([0.4, 0.0, 0.4]),
            table_z_min=0.05,
            base_radius_m=0.1,
            require_camera_boxes=True,
        )


def test_policy_path_validator_accepts_clear_tool_path():
    validator = validator_for(np.array([0.4, 0.0, 0.4]))
    assert validator(np.zeros((3, 7))) is None


@pytest.mark.parametrize(
    ("point", "message"),
    [
        (np.array([0.4, 0.0, 0.01]), "table exclusion"),
        (np.array([0.01, 0.01, 0.4]), "base exclusion"),
        (np.array([0.25, 0.25, 0.25]), "camera exclusion"),
        (np.array([np.nan, 0.0, 0.4]), "FK failed"),
    ],
)
def test_policy_path_validator_rejects_exclusion_volumes(point, message):
    assert message in validator_for(point)(np.zeros((2, 7)))
