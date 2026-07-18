from __future__ import annotations

import numpy as np

from armd.kinematics import KinematicsEngine, default_sdk_root


def test_engine_fk_jacobian_ik_and_cartesian_plan() -> None:
    engine = KinematicsEngine(sdk_root=default_sdk_root())
    reference = np.array([0.2, 0.6, 0.8, 0.1, -0.2, 0.1])
    fk = engine.forward_kinematics(reference)

    assert np.asarray(fk["position"]).shape == (3,)
    assert np.asarray(fk["rotation"]).shape == (3, 3)
    assert engine.jacobian(reference).shape == (6, 6)
    assert engine.manipulability(reference) >= 0.0

    solution = engine.inverse_kinematics(
        target_position=np.asarray(fk["position"]),
        target_rotation=np.asarray(fk["rotation"]),
        init_q=reference,
        max_iter=100,
        eps=1e-4,
        damping=1e-2,
        adaptive_damping=True,
        multi_init=False,
        num_attempts=1,
    )
    assert solution is not None
    assert np.linalg.norm(solution - reference) < 1e-6

    start = engine.forward_kinematics(np.zeros(6))
    target_position = np.asarray(start["position"]).copy()
    target_position[2] += 0.004
    trajectory, fraction = engine.compute_cartesian_path(
        current_q=np.zeros(6),
        waypoints=[
            {
                "position": np.asarray(start["position"]),
                "rotation": np.asarray(start["rotation"]),
            },
            {"position": target_position, "rotation": np.asarray(start["rotation"])},
        ],
    )
    assert fraction == 1.0
    assert len(trajectory) >= 2
    timestamps = engine.time_parameterization(trajectory, 0.2)
    positions, times, velocities = engine.smooth_trajectory(trajectory, timestamps)
    assert len(positions) == len(times) == len(velocities)
    assert times[-1] == 0.2


def test_trajectory_parameterization_enforces_velocity_and_acceleration_limits() -> None:
    engine = KinematicsEngine(sdk_root=default_sdk_root())
    trajectory = [np.zeros(6), np.array([0.3, 0.1, 0.1, 0.0, 0.0, 0.0])]

    positions, timestamps, velocities = engine.parameterize_trajectory(
        trajectory,
        duration=None,
        use_spline=True,
    )

    velocity_values = np.asarray(velocities)
    acceleration_values = np.gradient(velocity_values, np.asarray(timestamps), axis=0)
    assert np.all(np.abs(velocity_values) <= engine.velocity_limits + 1e-6)
    assert np.all(np.abs(acceleration_values) <= engine.acceleration_limits + 1e-6)
    assert len(positions) == len(timestamps)

    with np.testing.assert_raises_regex(ValueError, "duration_s 过短"):
        engine.parameterize_trajectory(trajectory, duration=0.01, use_spline=True)
