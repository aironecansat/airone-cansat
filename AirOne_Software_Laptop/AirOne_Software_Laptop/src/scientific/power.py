"""Power / battery analyses: voltage trend, health, brownout risk."""
from __future__ import annotations

from typing import Any

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "power"

_V_FIELDS = ("battery_voltage", "bus_voltage", "voltage", "vbat")


def voltage_trend(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Battery voltage trend over time (V/s)."""

    name = "voltage_trend"
    vf = series.first_present(*_V_FIELDS)
    if vf is None:
        return ScientificResult.insufficient(name, "no voltage field", category=CATEGORY)
    t, v = series.series(vf)
    fit = stats.linear_regression(t, v)
    if fit is None:
        return ScientificResult.insufficient(name, "fewer than 3 samples", n_samples=len(t), category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=fit["slope"],
        unit="V/s",
        method="OLS slope of voltage vs time",
        result_type=ResultType.DERIVED,
        inputs={"n": fit["n"], "r_squared": fit["r_squared"]},
        assumptions=["Approximately linear discharge over window"],
        uncertainty=fit["slope_stderr"],
        confidence=max(0.0, min(1.0, fit["r_squared"])),
        limitations=["Load-dependent; not a state-of-charge estimate"],
        n_samples=fit["n"],
        category=CATEGORY,
    )


def brownout_risk(series: SeriesBundle, min_voltage: float = 3.3, **_: Any) -> ScientificResult:
    """Assess brownout risk by extrapolating the voltage trend to a floor."""

    name = "brownout_risk"
    vf = series.first_present(*_V_FIELDS)
    if vf is None:
        return ScientificResult.insufficient(name, "no voltage field", category=CATEGORY)
    latest = series.latest(vf)
    trend = voltage_trend(series)
    if latest is None:
        return ScientificResult.insufficient(name, "no valid voltage", category=CATEGORY)
    slope = float(trend.value) if trend.valid and trend.value is not None else 0.0
    if latest.value <= min_voltage:
        risk = "critical"
        eta = 0.0
    elif slope >= 0:
        risk = "low"
        eta = None
    else:
        eta = (latest.value - min_voltage) / (-slope)
        risk = "high" if eta < 60 else ("elevated" if eta < 300 else "low")
    return ScientificResult(
        analysis_name=name,
        value=risk,
        unit="",
        method=f"Linear extrapolation of voltage to {min_voltage} V floor",
        result_type=ResultType.PREDICTED,
        inputs={"current_voltage": latest.value, "slope_V_per_s": slope, "eta_s": eta},
        assumptions=["Discharge continues at the current linear rate"],
        uncertainty=None,
        confidence=0.4,
        limitations=["Battery discharge is nonlinear near the knee"],
        n_samples=trend.n_samples,
        category=CATEGORY,
    )


def battery_health(series: SeriesBundle, nominal: float = 4.2, minimum: float = 3.0, **_: Any) -> ScientificResult:
    """Rough state-of-charge proxy from the latest voltage (linear map)."""

    name = "battery_health"
    vf = series.first_present(*_V_FIELDS)
    if vf is None:
        return ScientificResult.insufficient(name, "no voltage field", category=CATEGORY)
    m = series.latest(vf)
    if m is None:
        return ScientificResult.insufficient(name, "no valid voltage", category=CATEGORY)
    soc = max(0.0, min(1.0, (m.value - minimum) / (nominal - minimum))) * 100.0
    return ScientificResult(
        analysis_name=name,
        value=soc,
        unit="% (proxy)",
        method="Linear voltage-to-SoC map between minimum and nominal",
        result_type=ResultType.MODELLED,
        inputs={"voltage": m.value, "nominal": nominal, "minimum": minimum},
        assumptions=[f"Linear SoC between {minimum} V and {nominal} V"],
        uncertainty=None,
        confidence=0.3,
        limitations=["Crude proxy; real SoC needs a discharge curve / coulomb counting"],
        n_samples=1,
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("voltage_trend", CATEGORY, voltage_trend, "Battery voltage trend", [], 3)
    reg.register("brownout_risk", CATEGORY, brownout_risk, "Brownout risk", [], 1)
    reg.register("battery_health", CATEGORY, battery_health, "Battery SoC proxy", [], 1)
