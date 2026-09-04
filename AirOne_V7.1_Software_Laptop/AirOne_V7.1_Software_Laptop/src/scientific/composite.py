"""Composite / multi-variable analyses.

Includes a correlation matrix (all CORRELATION-typed, so each carries the
causation caveat), an environmental hazard proxy, and a change-rate monitor.
"""
from __future__ import annotations

import itertools
from typing import Any, List

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "composite"

_DEFAULT_FIELDS = (
    "temperature", "pressure", "altitude", "humidity", "uv_index",
    "radiation_cpm", "voc_index", "battery_voltage",
)


def correlation_matrix(series: SeriesBundle, fields: List[str] = None, **_: Any) -> ScientificResult:
    """Pairwise Pearson correlations among available numeric fields."""

    name = "correlation_matrix"
    candidates = fields or [f for f in _DEFAULT_FIELDS if series.quality_of(f)["valid"] >= 3]
    if len(candidates) < 2:
        return ScientificResult.insufficient(name, "need >=2 fields with >=3 valid samples", category=CATEGORY)
    matrix = {}
    for a, b in itertools.combinations(candidates, 2):
        # Build paired vectors on frames where both valid.
        xs, ys = [], []
        for f in series._frames:
            ma = f.measurements.get(a)
            mb = f.measurements.get(b)
            if ma and mb and ma.valid and mb.valid:
                xs.append(float(ma.value))
                ys.append(float(mb.value))
        pr = stats.pearson_r(xs, ys)
        if pr is not None:
            matrix[f"{a}~{b}"] = {"r": pr[0], "n": pr[1]}
    if not matrix:
        return ScientificResult.insufficient(name, "no field pair had >=3 concurrent valid samples", category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=matrix,
        unit="",
        method="Pairwise Pearson correlation coefficients",
        result_type=ResultType.CORRELATION,
        inputs={"fields": candidates},
        assumptions=["Linear association model"],
        uncertainty=None,
        confidence=0.6,
        limitations=["Associations only; confounders not controlled"],
        n_samples=series.n_frames,
        category=CATEGORY,
    )


def environmental_hazard_index(series: SeriesBundle, **_: Any) -> ScientificResult:
    """A transparent, weighted hazard proxy combining UV, radiation, air quality.

    Each component is normalised to 0..1 and weighted. This is a decision-aid
    proxy, explicitly MODELLED, not a certified hazard rating.
    """

    name = "environmental_hazard_index"
    components = {}
    weights = {}

    uv = series.latest("uv_index")
    if uv is not None:
        components["uv"] = min(1.0, max(0.0, uv.value / 11.0))
        weights["uv"] = 0.35
    rad = series.latest("radiation_cpm") or series.latest("dose_rate")
    if rad is not None:
        # Normalise CPM against a nominal 100 CPM ceiling for the proxy.
        components["radiation"] = min(1.0, max(0.0, rad.value / 100.0))
        weights["radiation"] = 0.4
    voc = series.latest("voc_index")
    if voc is not None:
        components["air_quality"] = min(1.0, max(0.0, (voc.value - 100.0) / 400.0))
        weights["air_quality"] = 0.25

    if not components:
        return ScientificResult.insufficient(name, "no hazard inputs available", category=CATEGORY)
    wsum = sum(weights.values())
    index = sum(components[k] * weights[k] for k in components) / wsum * 100.0
    return ScientificResult(
        analysis_name=name,
        value=index,
        unit="proxy 0-100",
        method="Weighted sum of normalised UV/radiation/air-quality components",
        result_type=ResultType.MODELLED,
        inputs={"components": components, "weights": weights},
        assumptions=["Component normalisation ceilings are indicative"],
        uncertainty=None,
        confidence=0.35,
        limitations=[
            "Decision-aid proxy, NOT a certified hazard rating",
            "Missing components are simply excluded (renormalised)",
        ],
        n_samples=series.n_frames,
        category=CATEGORY,
    )


def change_rate_monitor(series: SeriesBundle, field: str = "pressure", **_: Any) -> ScientificResult:
    """Report the rate of change of a chosen field (per second)."""

    name = "change_rate_monitor"
    resolved = series.first_present(field, f"fused_{field}", f"bme688_{field}")
    if resolved is None:
        return ScientificResult.insufficient(name, f"field '{field}' not present", category=CATEGORY)
    t, v = series.series(resolved)
    fit = stats.linear_regression(t, v)
    if fit is None:
        return ScientificResult.insufficient(name, "fewer than 3 samples", n_samples=len(t), category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=fit["slope"],
        unit="per_s",
        method=f"OLS slope of {resolved} vs time",
        result_type=ResultType.DERIVED,
        inputs={"field": resolved, "n": fit["n"], "r_squared": fit["r_squared"]},
        assumptions=["Linear over window"],
        uncertainty=fit["slope_stderr"],
        confidence=max(0.0, min(1.0, fit["r_squared"])),
        limitations=["Window-local rate"],
        n_samples=fit["n"],
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("correlation_matrix", CATEGORY, correlation_matrix, "Pairwise correlations", [], 3)
    reg.register("environmental_hazard_index", CATEGORY, environmental_hazard_index, "Hazard proxy 0-100", [], 1)
    reg.register("change_rate_monitor", CATEGORY, change_rate_monitor, "Field change rate", [], 3)
