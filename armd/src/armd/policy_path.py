"""Conservative task-space exclusion volumes for real policy execution."""

from __future__ import annotations

import numpy as np


class ConservativePolicyPathValidator:
    """Reject sampled joint paths whose tool point enters configured volumes.

    This is intentionally conservative and only admits hardware policy motion
    when a real FK callable and explicit workspace geometry are supplied.
    """

    def __init__(
        self,
        *,
        forward_kinematics,
        table_z_min: float,
        base_center_xy: tuple[float, float] = (0.0, 0.0),
        base_radius_m: float = 0.16,
        camera_boxes: tuple[tuple[tuple[float, float, float], tuple[float, float, float]], ...] = (),
        require_camera_boxes: bool = False,
    ) -> None:
        if not np.isfinite(table_z_min):
            raise ValueError("table_z_min must be finite")
        if not np.isfinite(base_radius_m) or base_radius_m <= 0:
            raise ValueError("base_radius_m must be positive and finite")
        self.forward_kinematics = forward_kinematics
        self.table_z_min = float(table_z_min)
        self.base_center_xy = np.asarray(base_center_xy, dtype=np.float64)
        if self.base_center_xy.shape != (2,) or not np.isfinite(self.base_center_xy).all():
            raise ValueError("base_center_xy must contain two finite values")
        self.base_radius_m = float(base_radius_m)
        parsed_boxes = []
        for lower, upper in camera_boxes:
            low = np.asarray(lower, dtype=np.float64)
            high = np.asarray(upper, dtype=np.float64)
            if low.shape != (3,) or high.shape != (3,) or not np.isfinite([*low, *high]).all():
                raise ValueError("camera exclusion boxes must contain finite XYZ bounds")
            if np.any(low >= high):
                raise ValueError("camera exclusion box lower bounds must be below upper bounds")
            parsed_boxes.append((low, high))
        self.camera_boxes = tuple(parsed_boxes)
        if require_camera_boxes and not self.camera_boxes:
            raise ValueError("hardware policy requires at least one calibrated camera exclusion box")

    def __call__(self, sampled_positions: np.ndarray) -> str | None:
        samples = np.asarray(sampled_positions, dtype=np.float64)
        if samples.ndim != 2 or samples.shape[1] != 7 or not np.isfinite(samples).all():
            return "invalid sampled joint path"
        for index, joints in enumerate(samples[:, :6]):
            tool = np.asarray(self.forward_kinematics(joints), dtype=np.float64)
            if tool.shape != (3,) or not np.isfinite(tool).all():
                return f"FK failed at sampled path index {index}"
            if tool[2] < self.table_z_min:
                return f"tool point enters table exclusion half-space at sample {index}"
            if np.linalg.norm(tool[:2] - self.base_center_xy) < self.base_radius_m:
                return f"tool point enters base exclusion cylinder at sample {index}"
            for box_index, (lower, upper) in enumerate(self.camera_boxes):
                if np.all(tool >= lower) and np.all(tool <= upper):
                    return f"tool point enters camera exclusion box {box_index} at sample {index}"
        return None
