"""Device-clock to Pi-monotonic affine fitting with explicit quality metrics."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class AffineClockFit:
    slope: float
    intercept_ns: float
    expected_slope: float
    drift_ppm: float
    residual_p50_ns: float
    residual_p95_ns: float
    residual_max_abs_ns: float
    sample_count: int

    def map(self, device_timestamp: float) -> int:
        return round(self.slope * float(device_timestamp) + self.intercept_ns)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "slope": self.slope,
            "intercept_ns": self.intercept_ns,
            "expected_slope": self.expected_slope,
            "drift_ppm": self.drift_ppm,
            "residual_p50_ns": self.residual_p50_ns,
            "residual_p95_ns": self.residual_p95_ns,
            "residual_max_abs_ns": self.residual_max_abs_ns,
            "sample_count": self.sample_count,
        }


def fit_affine_clock(
    device_timestamps: list[float],
    host_monotonic_ns: list[int],
    *,
    expected_ns_per_device_unit: float,
) -> AffineClockFit:
    if len(device_timestamps) != len(host_monotonic_ns) or len(device_timestamps) < 3:
        raise ValueError("clock fitting requires at least three paired timestamps")
    device = np.asarray(device_timestamps, dtype=np.float64)
    host = np.asarray(host_monotonic_ns, dtype=np.float64)
    if not np.isfinite(device).all() or not np.isfinite(host).all():
        raise ValueError("clock fit timestamps must be finite")
    if np.any(np.diff(device) <= 0) or np.any(np.diff(host) <= 0):
        raise ValueError("clock fit timestamps must be strictly increasing")
    if not np.isfinite(expected_ns_per_device_unit) or expected_ns_per_device_unit <= 0:
        raise ValueError("expected clock scale must be positive and finite")

    device_origin = device[0]
    host_origin = host[0]
    slope, centered_intercept = np.polyfit(device - device_origin, host - host_origin, 1)
    intercept = host_origin + centered_intercept - slope * device_origin
    predicted = slope * device + intercept
    residual = host - predicted
    absolute = np.abs(residual)
    return AffineClockFit(
        slope=float(slope),
        intercept_ns=float(intercept),
        expected_slope=float(expected_ns_per_device_unit),
        drift_ppm=float((slope / expected_ns_per_device_unit - 1.0) * 1_000_000),
        residual_p50_ns=float(np.percentile(absolute, 50)),
        residual_p95_ns=float(np.percentile(absolute, 95)),
        residual_max_abs_ns=float(absolute.max()),
        sample_count=len(device_timestamps),
    )
