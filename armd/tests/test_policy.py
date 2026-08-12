from __future__ import annotations

import time

import numpy as np
import pytest

from armd.backend import DEFAULT_LIMITS, MotorSnapshot, SimBackend
from armd.hardware_loop import CachedRobotState, HardwareLoop, MotionStepResult
from armd.policy import (
    PolicyChunkMotion,
    PolicySafetyConfig,
    PolicyValidationError,
    validate_policy_chunk,
)

HASH = "a" * 64


def cached_state(
    *,
    sampled_ns: int,
    sequence: int = 10,
    stream_id: str = "stream-a",
    positions: tuple[float, ...] = (0.0,) * 7,
):
    motors = tuple(
        MotorSnapshot(
            name=f"joint{index + 1}",
            motor_id=index + 1,
            position=positions[index],
            velocity=0.0,
            torque=0.0,
            motor_time=1.0,
            mode=0,
            fault=0,
        )
        for index in range(7)
    )
    return CachedRobotState(
        motors=motors,
        refreshed_at=sampled_ns / 1e9,
        sampled_monotonic_ns=sampled_ns,
        sequence=sequence,
        stream_instance_id=stream_id,
    )


def valid_kwargs(now_ns: int):
    return {
        "request_id": "request-1",
        "session_id": "session-1",
        "observation_sequence": 10,
        "observation_sampled_monotonic_ns": now_ns - 1_000_000,
        "state_stream_instance_id": "stream-a",
        "deadline_pi_monotonic_ns": now_ns + 1_000_000_000,
        "waypoint_positions": [[0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01]],
        "step_offsets_ns": [100_000_000],
        "checkpoint_sha256": HASH,
        "stats_sha256": HASH,
        "schema_sha256": HASH,
        "cached_state": cached_state(sampled_ns=now_ns - 1_000_000),
        "observation_state": cached_state(sampled_ns=now_ns - 1_000_000),
        "limits": DEFAULT_LIMITS,
        "now_monotonic_ns": now_ns,
    }


def test_policy_trajectory_preserves_30hz_waypoints_on_200hz_clock():
    now_ns = 10_000_000_000
    kwargs = valid_kwargs(now_ns)
    kwargs["waypoint_positions"] = [
        [0.002, 0.0, 0.0, 0.0, 0.0, 0.0, 0.002],
        [0.004, 0.0, 0.0, 0.0, 0.0, 0.0, 0.004],
    ]
    kwargs["step_offsets_ns"] = [33_333_333, 66_666_667]
    chunk = validate_policy_chunk(**kwargs)

    assert np.allclose(chunk.trajectory(0.033333333), kwargs["waypoint_positions"][0])
    assert np.allclose(chunk.trajectory(0.066666667), kwargs["waypoint_positions"][1])
    assert chunk.metrics.max_joint_delta == pytest.approx(0.004)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("waypoint_positions", [[float("nan")] * 7], "NaN or Inf"),
        ("waypoint_positions", [[0.0] * 6], "must have shape"),
        ("state_stream_instance_id", "wrong", "stream instance mismatch"),
        ("observation_sampled_monotonic_ns", 1, "does not match"),
        ("deadline_pi_monotonic_ns", 1, "deadline has expired"),
        ("checkpoint_sha256", "not-a-hash", "lowercase SHA-256"),
        (
            "waypoint_positions",
            [[DEFAULT_LIMITS.joint_upper[0] + 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
            "position soft limit",
        ),
    ],
)
def test_policy_validator_rejects_invalid_chunks(field, value, message):
    now_ns = 10_000_000_000
    kwargs = valid_kwargs(now_ns)
    kwargs[field] = value
    with pytest.raises(PolicyValidationError, match=message):
        validate_policy_chunk(**kwargs)


def test_policy_validator_allows_small_startup_measurement_error_but_rejects_large_violation():
    now_ns = 10_000_000_000
    kwargs = valid_kwargs(now_ns)
    slightly_low = cached_state(
        sampled_ns=now_ns - 1_000_000,
        positions=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -0.0085),
    )
    kwargs["cached_state"] = slightly_low
    kwargs["observation_state"] = slightly_low
    validate_policy_chunk(**kwargs)

    too_low = cached_state(
        sampled_ns=now_ns - 1_000_000,
        positions=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -0.011),
    )
    kwargs["cached_state"] = too_low
    kwargs["observation_state"] = too_low
    with pytest.raises(PolicyValidationError, match="startup measurement tolerance"):
        validate_policy_chunk(**kwargs)


def test_policy_validator_binds_observation_sequence_to_retained_timestamp():
    now_ns = 10_000_000_000
    kwargs = valid_kwargs(now_ns)
    kwargs["observation_sampled_monotonic_ns"] = now_ns - 500_000
    with pytest.raises(PolicyValidationError, match="does not match"):
        validate_policy_chunk(**kwargs)

    kwargs = valid_kwargs(now_ns)
    kwargs["observation_state"] = None
    with pytest.raises(PolicyValidationError, match="no longer retained"):
        validate_policy_chunk(**kwargs)


def test_policy_validator_rejects_unsafe_dynamics_and_swept_path():
    now_ns = 10_000_000_000
    kwargs = valid_kwargs(now_ns)
    kwargs["waypoint_positions"] = [[0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]
    kwargs["step_offsets_ns"] = [10_000_000]
    with pytest.raises(PolicyValidationError, match="velocity limit"):
        validate_policy_chunk(**kwargs)

    kwargs = valid_kwargs(now_ns)
    with pytest.raises(PolicyValidationError, match="swept path rejected"):
        validate_policy_chunk(**kwargs, path_validator=lambda _: "camera exclusion box")


def test_policy_motion_reports_cartesian_endpoint_acceptance():
    now_ns = 10_000_000_000
    kwargs = valid_kwargs(now_ns)
    chunk = validate_policy_chunk(**kwargs)
    motion = PolicyChunkMotion(
        chunk,
        forward_kinematics=lambda joints: np.array([joints[0], joints[1], joints[2]]),
    )
    assert motion.endpoint_error_m is None


def test_policy_motion_runs_as_atomic_frames_and_holds_endpoint():
    backend = SimBackend()
    loop = HardwareLoop(lambda: backend, control_hz=200.0)
    loop.start()
    try:
        deadline = time.monotonic_ns() + 2_000_000_000
        for _ in range(100):
            cached = loop.latest_state()
            if cached is not None:
                break
            time.sleep(0.005)
        assert cached is not None
        chunk = validate_policy_chunk(
            request_id="motion-1",
            session_id="session-1",
            observation_sequence=cached.sequence,
            observation_sampled_monotonic_ns=cached.sampled_monotonic_ns,
            state_stream_instance_id=cached.stream_instance_id,
            deadline_pi_monotonic_ns=deadline,
            waypoint_positions=[[0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01]],
            step_offsets_ns=[100_000_000],
            checkpoint_sha256=HASH,
            stats_sha256=HASH,
            schema_sha256=HASH,
            cached_state=cached,
            observation_state=cached,
            limits=DEFAULT_LIMITS,
            now_monotonic_ns=time.monotonic_ns(),
            config=PolicySafetyConfig(endpoint_hold_s=0.02),
        )
        motion = PolicyChunkMotion(
            chunk,
            forward_kinematics=lambda joints: np.array([joints[0], joints[1], joints[2]]),
        )
        accepted, completion = loop.start_motion_with_ack(motion)
        accepted.result(timeout=1)
        assert completion.result(timeout=2) is MotionStepResult.DONE
        states = loop.submit(lambda active: active.read_all()).result(timeout=1)
        assert [state.position for state in states[:6]] == pytest.approx(chunk.waypoints[-1, :6], abs=0.003)
        assert states[6].position == pytest.approx(chunk.waypoints[-1, 6], abs=0.003)
        assert motion.fraction == 1.0
        assert motion.endpoint_error_m is not None
        assert motion.endpoint_error_m <= 0.03
    finally:
        loop.stop()
