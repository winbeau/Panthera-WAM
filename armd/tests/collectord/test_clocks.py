from __future__ import annotations

import pytest

from armd.collectord.clocks import fit_affine_clock


def test_affine_clock_fit_recovers_offset_and_drift() -> None:
    device_ms = [float(index * 10) for index in range(20)]
    expected_scale = 1_000_000.0
    true_scale = expected_scale * 1.000025
    host_ns = [round(5_000_000_000 + true_scale * value) for value in device_ms]

    fit = fit_affine_clock(
        device_ms,
        host_ns,
        expected_ns_per_device_unit=expected_scale,
    )

    assert fit.slope == pytest.approx(true_scale, rel=1e-10)
    assert fit.intercept_ns == pytest.approx(5_000_000_000, abs=1)
    assert fit.drift_ppm == pytest.approx(25.0, abs=1e-5)
    assert fit.residual_max_abs_ns <= 1.0
    assert fit.map(250.0) == pytest.approx(5_000_000_000 + true_scale * 250.0, abs=1)


def test_affine_clock_fit_rejects_regression() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        fit_affine_clock(
            [0.0, 10.0, 5.0],
            [1_000, 2_000, 3_000],
            expected_ns_per_device_unit=1_000_000.0,
        )
