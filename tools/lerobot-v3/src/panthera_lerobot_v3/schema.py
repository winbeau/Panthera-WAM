from __future__ import annotations

from typing import Final

LEROBOT_VERSION: Final = "0.4.4"
LEROBOT_CODEBASE_VERSION: Final = "v3.0"
SCHEMA_VERSION: Final = "panthera-fastwam-v1"
FPS: Final = 30
AXES: Final = (
    "joint_1",
    "joint_2",
    "joint_3",
    "joint_4",
    "joint_5",
    "joint_6",
    "gripper",
)
ACTION_SEMANTICS: Final = "next_absolute_position_waypoint_q_t_plus_1_30hz"
STATE_UNITS: Final = (
    "rad",
    "rad",
    "rad",
    "rad",
    "rad",
    "rad",
    "native_gripper_position",
)
CAMERA_ORDER: Final = ("overhead_rgb", "wrist_rgb")
DEPTH_POLICY: Final = "sidecar_only_not_fastwam_rgb_input"


def schema_identity() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "lerobot_version": LEROBOT_VERSION,
        "lerobot_codebase_version": LEROBOT_CODEBASE_VERSION,
        "fps": FPS,
        "axes": list(AXES),
        "state_units": list(STATE_UNITS),
        "action_units": list(STATE_UNITS),
        "action_semantics": ACTION_SEMANTICS,
        "camera_order": list(CAMERA_ORDER),
        "color_space": "RGB",
        "depth_policy": DEPTH_POLICY,
    }
