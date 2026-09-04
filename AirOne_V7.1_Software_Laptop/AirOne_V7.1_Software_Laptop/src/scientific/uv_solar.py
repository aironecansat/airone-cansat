"""UV and solar analyses (VEML6075 UVA/UVB, OPT3001 lux).

The VEML6075 UV index uses the manufacturer's recommended coefficients to
convert compensated UVA/UVB counts to a UV index. Where the fused uv_index is
already present it is used directly and labelled MEASURED.
"""
from __future__ import annotations

import math
from typing import Any

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "uv_solar"

# VEML6075 application-note coefficients.
UVA_A_COEF = 2.22
UVA_B_COEF = 1.33
UVB_C_COEF = 2.95
UVB_D_COEF = 1.74
UVA_RESPONSIVITY = 0.001461
UVB_RESPONSIVITY = 0.002591


def uv_index(series: SeriesBundle, **_: Any) -> ScientificResult:
    """UV index from VEML6075 UVA/UVB (or direct fused uv_index)."""

    name = "uv_index"
    direct = series.first_present("uv_index", "fused_uv_index", "veml6075_uv_index")
    if direct is not None:
        m = series.latest(direct)
        if m is not None:
            return ScientificResult(
                analysis_name=name, value=m.value, unit="UVI",
                method="Fused/direct UV index telemetry",
                result_type=ResultType.MEASURED, inputs={"field": direct},
                assumptions=["Sensor pre-compensated"],
                uncertainty=m.uncertainty if math.isfinite(m.uncertainty) else None,
                confidence=0.85, limitations=[], n_samples=1, category=CATEGORY,
            )
    uaf = series.first_present("uva", "veml6075_uva")
    ubf = series.first_present("uvb", "veml6075_uvb")
    if uaf is None or ubf is None:
        return ScientificResult.insufficient(name, "need UVA and UVB", category=CATEGORY)
    ua = series.latest(uaf)
    ub = series.latest(ubf)
    if ua is None or ub is None:
        return ScientificResult.insufficient(name, "no valid UVA/UVB", category=CATEGORY)
    # Simplified VEML6075 UVI (visible/IR compensation assumed applied upstream).
    uvia = ua.value * UVA_RESPONSIVITY
    uvib = ub.value * UVB_RESPONSIVITY
    uvi = max(0.0, (uvia + uvib) / 2.0)
    return ScientificResult(
        analysis_name=name,
        value=uvi,
        unit="UVI",
        method="VEML6075 UVI = (UVA*ra + UVB*rb)/2 with app-note responsivities",
        result_type=ResultType.MODELLED,
        inputs={"uva": ua.value, "uvb": ub.value},
        assumptions=[
            "Visible/IR compensation applied upstream",
            f"Responsivities ra={UVA_RESPONSIVITY}, rb={UVB_RESPONSIVITY}",
        ],
        uncertainty=None,
        confidence=0.6,
        limitations=["Uncalibrated against a reference radiometer"],
        n_samples=1,
        category=CATEGORY,
    )


def uv_altitude_trend(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Trend of UV index with altitude (correlation, not causation)."""

    name = "uv_altitude_trend"
    uf = series.first_present("uv_index", "fused_uv_index")
    af = series.first_present("altitude", "fused_altitude", "gnss_altitude")
    if uf is None or af is None:
        return ScientificResult.insufficient(name, "need uv_index and altitude", category=CATEGORY)
    xs, ys = [], []
    for f in series._frames:
        mu = f.measurements.get(uf)
        ma = f.measurements.get(af)
        if mu is None or ma is None or not mu.valid or not ma.valid:
            continue
        xs.append(float(ma.value))
        ys.append(float(mu.value))
    fit = stats.linear_regression(xs, ys)
    if fit is None:
        return ScientificResult.insufficient(name, "fewer than 3 paired samples", n_samples=len(xs), category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=fit["r"],
        unit="",
        method="Pearson correlation of UV index vs altitude",
        result_type=ResultType.CORRELATION,
        inputs={"slope_UVI_per_m": fit["slope"], "n": fit["n"]},
        assumptions=["Linear association"],
        uncertainty=None,
        confidence=min(1.0, abs(fit["r"])),
        limitations=["Association only; clouds/aerosols not controlled"],
        n_samples=fit["n"],
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("uv_index", CATEGORY, uv_index, "UV index (VEML6075)", [], 1)
    reg.register("uv_altitude_trend", CATEGORY, uv_altitude_trend,
                 "UV vs altitude correlation", [], 3)
