"""armd 硬件后端。"""

from .base import (
    Backend,
    BackendClosedError,
    BackendError,
    BackendLimits,
    DEFAULT_LIMITS,
    DISCONNECTED_SENTINEL,
    FrameMode,
    JointFrame,
    LimitViolationError,
    MotorSnapshot,
    STALE_AFTER_S,
)
from .sim import SimBackend
from .real import (
    DEFAULT_MOTOR_TIMEOUT_MS,
    MIN_SAFE_STATE_QUERY_FIRMWARE,
    RealBackend,
    SdkAuditError,
    SdkAuditResult,
    audit_sdk_source,
)

__all__ = [
    "Backend",
    "BackendClosedError",
    "BackendError",
    "BackendLimits",
    "DEFAULT_LIMITS",
    "DISCONNECTED_SENTINEL",
    "FrameMode",
    "JointFrame",
    "LimitViolationError",
    "MotorSnapshot",
    "STALE_AFTER_S",
    "SimBackend",
    "DEFAULT_MOTOR_TIMEOUT_MS",
    "MIN_SAFE_STATE_QUERY_FIRMWARE",
    "RealBackend",
    "SdkAuditError",
    "SdkAuditResult",
    "audit_sdk_source",
]
