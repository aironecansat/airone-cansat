"""Descent and recovery analyses: descent rate, terminal velocity, drag.

These support the primary CanSat mission requirement of characterising the
descent. Formulae use the drag equation and standard atmosphere density.
"""
from __future__ import annotations

import math
from typing import Any

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "descent"

G0 = 9.80665


def _altitude_series(series: SeriesBundle):
    af = series.first_present("altitude", "fused_altitude", "gnss_altitude")
    if af is None:
        return None, [], []
    t, z = series.series(af)
    return af, t, z


def descent_rate(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Mean descent rate (positive = falling) during the descending window."""

    name = "descent_rate"
    af, t, z = _altitude_series(series)
    if af is None:
        return ScientificResult.insufficient(name, "no altitude field", category=CATEGORY)
    fit = stats.linear_regression(t, z)
    if fit is None:
        return ScientificResult.insufficient(name, "fewer than 3 samples", n_samples=len(t), category=CATEGORY)
    rate = -fit["slope"]  # positive when altitude decreasing
    return ScientificResult(
        analysis_name=name,
        value=rate,
        unit="m/s",
        method="Negative OLS slope of altitude vs time",
        result_type=ResultType.DERIVED,
        inputs={"n": fit["n"], "r_squared": fit["r_squared"]},
        assumptions=["Window covers the descent phase"],
        uncertainty=fit["slope_stderr"],
        confidence=max(0.0, min(1.0, fit["r_squared"])),
        limitations=["Mean over window; not instantaneous", "Negative value means still ascending"],
        n_samples=fit["n"],
        category=CATEGORY,
    )


def terminal_velocity(series: SeriesBundle, tolerance: float = 1.0, **_: Any) -> ScientificResult:
    """Detect terminal velocity as a late-window plateau in descent rate.

    Splits the descending window in half and checks whether the mean descent
    speed of the two halves agrees within ``tolerance`` m/s.
    """

    name = "terminal_velocity"
    af, t, z = _altitude_series(series)
    if af is None or len(z) < 6:
        return ScientificResult.insufficient(name, "need >=6 altitude samples", n_samples=len(z), category=CATEGORY)
    half = len(z) // 2
    f1 = stats.linear_regression(t[:half], z[:half])
    f2 = stats.linear_regression(t[half:], z[half:])
    if f1 is None or f2 is None:
        return ScientificResult.insufficient(name, "degenerate half-windows", n_samples=len(z), category=CATEGORY)
    v1 = -f1["slope"]
    v2 = -f2["slope"]
    reached = abs(v1 - v2) <= tolerance and v2 > 0
    return ScientificResult(
        analysis_name=name,
        value=v2 if reached else None,
        unit="m/s",
        method="Two-half descent-rate plateau test",
        result_type=ResultType.INFERRED,
        inputs={"v_first_half": v1, "v_second_half": v2, "tolerance": tolerance},
        assumptions=["Constant drag configuration (parachute deployed)"],
        uncertainty=abs(v1 - v2),
        confidence=0.6 if reached else 0.2,
        limitations=["Short windows cannot confirm a true plateau"],
        valid=reached,
        n_samples=len(z),
        category=CATEGORY,
    )


def drag_coefficient(series: SeriesBundle, mass_kg: float = 0.35, area_m2: float = 0.5, **_: Any) -> ScientificResult:
    """Estimate Cd from terminal velocity: Cd = 2*m*g / (rho*A*v^2)."""

    name = "drag_coefficient"
    tv = terminal_velocity(series)
    if not tv.valid or not tv.value:
        return ScientificResult.insufficient(name, "no terminal velocity established", n_samples=tv.n_samples, category=CATEGORY)
    v = float(tv.value)
    # Density: use latest atmospheric density if available, else sea-level ISA.
    rho = 1.225
    dens_field = series.first_present("air_density", "density")
    if dens_field is not None:
        dm = series.latest(dens_field)
        if dm is not None and dm.value > 0:
            rho = dm.value
    if v <= 0:
        return ScientificResult.insufficient(name, "non-positive terminal velocity", category=CATEGORY)
    cd = (2 * mass_kg * G0) / (rho * area_m2 * v ** 2)
    return ScientificResult(
        analysis_name=name,
        value=cd,
        unit="",
        method="Cd = 2*m*g / (rho*A*v_terminal^2)",
        result_type=ResultType.MODELLED,
        inputs={"mass_kg": mass_kg, "area_m2": area_m2, "rho_kg_m3": rho, "v_terminal": v},
        assumptions=[
            f"CanSat mass {mass_kg} kg, reference area {area_m2} m^2 (caller-supplied)",
            "Steady-state terminal descent",
        ],
        uncertainty=None,
        confidence=0.4,
        limitations=["Highly sensitive to assumed mass/area and density"],
        n_samples=tv.n_samples,
        category=CATEGORY,
    )


def landing_detection(series: SeriesBundle, still_threshold: float = 0.5, **_: Any) -> ScientificResult:
    """Detect landing as a sustained near-zero descent rate at low altitude."""

    name = "landing_detection"
    af, t, z = _altitude_series(series)
    if af is None or len(z) < 4:
        return ScientificResult.insufficient(name, "need >=4 altitude samples", n_samples=len(z), category=CATEGORY)
    tail = z[-4:]
    spread = max(tail) - min(tail)
    landed = spread <= still_threshold
    return ScientificResult(
        analysis_name=name,
        value=bool(landed),
        unit="",
        method=f"Altitude spread of last 4 samples <= {still_threshold} m",
        result_type=ResultType.INFERRED,
        inputs={"tail_spread_m": spread, "last_altitude_m": z[-1]},
        assumptions=["Altitude noise below the threshold when at rest"],
        uncertainty=None,
        confidence=0.7 if landed else 0.3,
        limitations=["Cannot distinguish landing from a hover/snag"],
        n_samples=len(z),
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("descent_rate", CATEGORY, descent_rate, "Mean descent rate", [], 3)
    reg.register("terminal_velocity", CATEGORY, terminal_velocity, "Terminal velocity plateau", [], 6)
    reg.register("drag_coefficient", CATEGORY, drag_coefficient, "Drag coefficient estimate", [], 6)
    reg.register("landing_detection", CATEGORY, landing_detection, "Landing detection", [], 4)
