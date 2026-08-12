"""Pi-domain policy chunk validation and atomic 30→200 Hz execution."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.interpolate import PchipInterpolator

from .backend import Backend, BackendLimits
from .hardware_loop import CachedRobotState, CancelReason, MotionStepResult
from .motion import hold_current_position, position_frame


class PolicyValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PolicySafetyConfig:
    control_hz: float = 200.0
    max_waypoints: int = 64
    max_horizon_s: float = 2.0
    max_observation_age_ns: int = 250_000_000
    min_first_offset_ns: int = 10_000_000
    endpoint_hold_s: float = 0.05
    joint_velocity_fraction: float = 0.5
    joint_acceleration_fraction: float = 0.5
    gripper_velocity_fraction: float = 0.5
    gripper_acceleration: float = 1.0
    jerk_ramp_hz: float = 10.0
    # Runtime tracking limits remain joint-space safety bounds. Human-facing
    # hardware acceptance is evaluated separately in Cartesian space (3 cm).
    joint_tracking_error: float = 0.20
    gripper_tracking_error: float = 0.20
    measured_limit_start_tolerance: float = 0.01
    hardware_endpoint_tolerance_m: float = 0.03
    arm_torque_fraction: float = 0.5
    gripper_torque_fraction: float = 0.5

    def __post_init__(self) -> None:
        positive = (
            self.control_hz,
            self.max_waypoints,
            self.max_horizon_s,
            self.max_observation_age_ns,
            self.min_first_offset_ns,
            self.endpoint_hold_s,
            self.gripper_acceleration,
            self.jerk_ramp_hz,
            self.joint_tracking_error,
            self.gripper_tracking_error,
            self.measured_limit_start_tolerance,
            self.hardware_endpoint_tolerance_m,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("policy safety configuration values must be positive")
        for value in (
            self.joint_velocity_fraction,
            self.joint_acceleration_fraction,
            self.gripper_velocity_fraction,
            self.arm_torque_fraction,
            self.gripper_torque_fraction,
        ):
            if not 0 < value <= 1:
                raise ValueError("policy safety fractions must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class PolicyMetrics:
    max_joint_delta: float
    max_gripper_delta: float
    sampled_max_velocity: float
    sampled_max_acceleration: float
    sampled_max_jerk: float


@dataclass(frozen=True, slots=True)
class ValidatedPolicyChunk:
    request_id: str
    session_id: str
    observation_sequence: int
    deadline_pi_monotonic_ns: int
    offsets_s: np.ndarray
    waypoints: np.ndarray
    trajectory: PchipInterpolator
    duration_s: float
    metrics: PolicyMetrics
    config: PolicySafetyConfig


def _require_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PolicyValidationError(f"{field} must be a lowercase SHA-256 hex digest")


def _finite_waypoints(values) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 7:
        raise PolicyValidationError(f"policy waypoints must have shape [T,7], got {array.shape}")
    if not np.isfinite(array).all():
        raise PolicyValidationError("policy waypoints contain NaN or Inf")
    return array


def _sample_derivatives(trajectory: PchipInterpolator, duration_s: float, control_hz: float):
    count = max(2, math.ceil(duration_s * control_hz) + 1)
    times = np.linspace(0.0, duration_s, count, dtype=np.float64)
    positions = np.asarray(trajectory(times), dtype=np.float64)
    edge_order = 2 if len(times) >= 3 else 1
    velocity = np.gradient(positions, times, axis=0, edge_order=edge_order)
    acceleration = np.gradient(velocity, times, axis=0, edge_order=edge_order)
    jerk = np.gradient(acceleration, times, axis=0, edge_order=edge_order)
    return times, positions, velocity, acceleration, jerk


def validate_policy_chunk(
    *,
    request_id: str,
    session_id: str,
    observation_sequence: int,
    observation_sampled_monotonic_ns: int,
    state_stream_instance_id: str,
    deadline_pi_monotonic_ns: int,
    waypoint_positions,
    step_offsets_ns,
    checkpoint_sha256: str,
    stats_sha256: str,
    schema_sha256: str,
    cached_state: CachedRobotState,
    observation_state: CachedRobotState | None = None,
    limits: BackendLimits,
    now_monotonic_ns: int,
    config: PolicySafetyConfig | None = None,
    path_validator: Callable[[np.ndarray], str | None] | None = None,
) -> ValidatedPolicyChunk:
    config = config or PolicySafetyConfig()
    if not request_id or not session_id:
        raise PolicyValidationError("request_id and session_id are required")
    _require_sha256(checkpoint_sha256, "checkpoint_sha256")
    _require_sha256(stats_sha256, "stats_sha256")
    _require_sha256(schema_sha256, "schema_sha256")
    if len(cached_state.motors) != 7 or not all(motor.valid for motor in cached_state.motors):
        raise PolicyValidationError("current seven-motor state is unavailable")
    anchor = np.asarray([motor.position for motor in cached_state.motors], dtype=np.float64)
    lower = np.concatenate([limits.joint_lower, [limits.gripper_lower]])
    upper = np.concatenate([limits.joint_upper, [limits.gripper_upper]])
    measured_below = lower - anchor
    measured_above = anchor - upper
    measured_violation = np.maximum(measured_below, measured_above)
    if np.any(measured_violation > config.measured_limit_start_tolerance):
        raise PolicyValidationError(
            "current measured state exceeds the startup measurement tolerance around soft limits"
        )
    if state_stream_instance_id != cached_state.stream_instance_id:
        raise PolicyValidationError("state stream instance mismatch")
    if observation_sequence <= 0 or observation_sequence > cached_state.sequence:
        raise PolicyValidationError("observation sequence is invalid or from the future")
    if observation_state is None or observation_state.sequence != observation_sequence:
        raise PolicyValidationError("observation sequence is no longer retained")
    if observation_state.stream_instance_id != state_stream_instance_id:
        raise PolicyValidationError("observation state stream instance mismatch")
    if observation_state.sampled_monotonic_ns != observation_sampled_monotonic_ns:
        raise PolicyValidationError("observation timestamp does not match its measured-state sequence")
    if observation_sampled_monotonic_ns <= 0 or observation_sampled_monotonic_ns > now_monotonic_ns:
        raise PolicyValidationError("observation timestamp is invalid or from the future")
    if now_monotonic_ns - observation_sampled_monotonic_ns > config.max_observation_age_ns:
        raise PolicyValidationError("observation is stale")
    if deadline_pi_monotonic_ns <= now_monotonic_ns:
        raise PolicyValidationError("policy response deadline has expired")

    waypoints = _finite_waypoints(waypoint_positions)
    if not 1 <= len(waypoints) <= config.max_waypoints:
        raise PolicyValidationError(f"policy chunk must contain 1..{config.max_waypoints} waypoints")
    offsets_ns = np.asarray(step_offsets_ns, dtype=np.int64)
    if offsets_ns.shape != (len(waypoints),):
        raise PolicyValidationError("step_offsets_ns length must match waypoints")
    if offsets_ns[0] < config.min_first_offset_ns or np.any(np.diff(offsets_ns) <= 0):
        raise PolicyValidationError("step offsets must be strictly increasing with sufficient lead time")
    duration_s = float(offsets_ns[-1]) / 1_000_000_000
    if duration_s > config.max_horizon_s:
        raise PolicyValidationError("policy chunk exceeds the maximum open-loop horizon")
    if now_monotonic_ns + offsets_ns[-1] + round(config.endpoint_hold_s * 1e9) > deadline_pi_monotonic_ns:
        raise PolicyValidationError("Pi deadline does not cover the complete chunk and endpoint hold")

    full_positions = np.vstack([anchor, waypoints])
    offsets_s = np.concatenate([[0.0], offsets_ns.astype(np.float64) / 1_000_000_000])
    trajectory = PchipInterpolator(offsets_s, full_positions, axis=0, extrapolate=False)
    _, sampled_position, sampled_velocity, sampled_acceleration, sampled_jerk = _sample_derivatives(
        trajectory,
        duration_s,
        config.control_hz,
    )

    velocity_limit = np.concatenate(
        [
            limits.joint_velocity * config.joint_velocity_fraction,
            [limits.gripper_velocity * config.gripper_velocity_fraction],
        ]
    )
    acceleration_limit = np.concatenate(
        [
            limits.joint_acceleration * config.joint_acceleration_fraction,
            [config.gripper_acceleration],
        ]
    )
    jerk_limit = acceleration_limit * config.jerk_ramp_hz
    # A measured anchor may be marginally outside a configured bound at startup.
    # Permit only a monotonic recovery toward the legal interval; every commanded
    # waypoint must itself remain inside the unmodified soft limits.
    if np.any(waypoints < lower) or np.any(waypoints > upper):
        raise PolicyValidationError("policy waypoint exceeds a position soft limit")
    recovery_lower = np.where(anchor < lower, anchor, lower)
    recovery_upper = np.where(anchor > upper, anchor, upper)
    if np.any(sampled_position < recovery_lower) or np.any(sampled_position > recovery_upper):
        raise PolicyValidationError("sampled policy path exceeds a position soft limit")
    if np.any(np.abs(sampled_velocity) > velocity_limit):
        raise PolicyValidationError("sampled policy path exceeds a velocity limit")
    if np.any(np.abs(sampled_acceleration) > acceleration_limit):
        raise PolicyValidationError("sampled policy path exceeds an acceleration limit")
    if np.any(np.abs(sampled_jerk) > jerk_limit):
        raise PolicyValidationError("sampled policy path exceeds a jerk limit")
    if path_validator is not None:
        reason = path_validator(sampled_position)
        if reason:
            raise PolicyValidationError(f"policy swept path rejected: {reason}")

    delta = np.abs(waypoints - anchor)
    metrics = PolicyMetrics(
        max_joint_delta=float(delta[:, :6].max()),
        max_gripper_delta=float(delta[:, 6].max()),
        sampled_max_velocity=float(np.abs(sampled_velocity).max()),
        sampled_max_acceleration=float(np.abs(sampled_acceleration).max()),
        sampled_max_jerk=float(np.abs(sampled_jerk).max()),
    )
    return ValidatedPolicyChunk(
        request_id=request_id,
        session_id=session_id,
        observation_sequence=observation_sequence,
        deadline_pi_monotonic_ns=deadline_pi_monotonic_ns,
        offsets_s=offsets_s,
        waypoints=waypoints,
        trajectory=trajectory,
        duration_s=duration_s,
        metrics=metrics,
        config=config,
    )


class PolicyChunkMotion:
    def __init__(
        self,
        chunk: ValidatedPolicyChunk,
        *,
        forward_kinematics: Callable[[np.ndarray], np.ndarray] | None = None,
    ) -> None:
        self.chunk = chunk
        self.reject_reason = ""
        self.endpoint_error_m: float | None = None
        self._forward_kinematics = forward_kinematics
        self._fraction = 0.0
        self._started_at: float | None = None
        self._hold_started_at: float | None = None
        self._cancel_reason: CancelReason | None = None
        self._lock = threading.Lock()

    @property
    def fraction(self) -> float:
        return self._fraction

    def request_cancel(self, reason: CancelReason) -> None:
        with self._lock:
            self._cancel_reason = reason
            self.reject_reason = f"policy chunk cancelled: {reason.value}"

    def step(self, backend: Backend, now: float) -> MotionStepResult:
        states = backend.read_all()
        if len(states) != 7 or not all(state.valid for state in states):
            backend.stop()
            self.reject_reason = "policy execution lost valid seven-motor feedback"
            return MotionStepResult.FAILED
        with self._lock:
            cancel_reason = self._cancel_reason
        if cancel_reason is not None:
            hold_current_position(backend)
            self.reject_reason = f"policy chunk cancelled: {cancel_reason.value}"
            return MotionStepResult.CANCELLED
        if round(now * 1_000_000_000) > self.chunk.deadline_pi_monotonic_ns:
            hold_current_position(backend)
            self.reject_reason = "policy chunk exceeded Pi monotonic deadline"
            return MotionStepResult.FAILED
        if self._started_at is None:
            self._started_at = now
        elapsed = max(0.0, now - self._started_at)
        sample_time = min(elapsed, self.chunk.duration_s)
        target = np.asarray(self.chunk.trajectory(sample_time), dtype=np.float64)
        velocity = np.asarray(self.chunk.trajectory.derivative(1)(sample_time), dtype=np.float64)
        measured = np.asarray([state.position for state in states], dtype=np.float64)
        tracking_limit = np.array(
            [self.chunk.config.joint_tracking_error] * 6 + [self.chunk.config.gripper_tracking_error],
            dtype=np.float64,
        )
        if np.any(np.abs(target - measured) > tracking_limit):
            hold_current_position(backend)
            self.reject_reason = "policy tracking error exceeded the configured limit"
            return MotionStepResult.FAILED

        backend.write_frame(
            position_frame(
                backend,
                arm_position=target[:6],
                arm_velocity=velocity[:6],
                arm_max_torque=backend.limits.joint_torque * self.chunk.config.arm_torque_fraction,
                gripper_position=float(target[6]),
                gripper_velocity=float(abs(velocity[6])),
                gripper_max_torque=backend.limits.gripper_torque * self.chunk.config.gripper_torque_fraction,
            )
        )
        self._fraction = min(1.0, elapsed / self.chunk.duration_s)
        if elapsed < self.chunk.duration_s:
            return MotionStepResult.RUNNING
        if self._hold_started_at is None:
            self._hold_started_at = now
            return MotionStepResult.RUNNING
        if now - self._hold_started_at < self.chunk.config.endpoint_hold_s:
            return MotionStepResult.RUNNING
        if self._forward_kinematics is not None:
            measured_endpoint = np.asarray(self._forward_kinematics(measured[:6]), dtype=np.float64)
            target_endpoint = np.asarray(
                self._forward_kinematics(self.chunk.waypoints[-1, :6]),
                dtype=np.float64,
            )
            if (
                measured_endpoint.shape != (3,)
                or target_endpoint.shape != (3,)
                or not np.isfinite(measured_endpoint).all()
                or not np.isfinite(target_endpoint).all()
            ):
                hold_current_position(backend)
                self.reject_reason = "policy endpoint FK is unavailable"
                return MotionStepResult.FAILED
            self.endpoint_error_m = float(np.linalg.norm(measured_endpoint - target_endpoint))
            if self.endpoint_error_m > self.chunk.config.hardware_endpoint_tolerance_m:
                hold_current_position(backend)
                self.reject_reason = (
                    "policy endpoint error exceeds "
                    f"{self.chunk.config.hardware_endpoint_tolerance_m:.3f} m tolerance"
                )
                return MotionStepResult.FAILED
        self._fraction = 1.0
        return MotionStepResult.DONE
