"""Atmospheric science analyses.

All formulae use SI units (pressure in Pa, temperature in K, altitude in m)
because the pipeline normalises measurements to SI before persistence. Each
function returns a :class:`ScientificResult` with explicit assumptions and
limitations. Where data are insufficient, ``ScientificResult.insufficient`` is
returned rather than a fabricated number.
"""
from __future__ import annotations

import math
from typing import Any

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "atmospheric"

# Physical constants.
R_SPECIFIC_DRY_AIR = 287.05  # J/(kg*K)
STANDARD_LAPSE_RATE = 0.0065  # K/m (ISA troposphere)
G0 = 9.80665  # m/s^2
P0_SEA_LEVEL = 101325.0  # Pa
T0_SEA_LEVEL = 288.15  # K

_PRESSURE_FIELDS = ("pressure", "bme688_pressure", "bmp581_pressure")
_TEMP_FIELDS = ("temperature", "bme688_temperature", "bmp581_temperature")
_ALT_FIELDS = ("altitude", "fused_altitude", "gnss_altitude")


def _pressure_field(series: SeriesBundle):
    return series.first_present(*_PRESSURE_FIELDS)


def _temp_field(series: SeriesBundle):
    return series.first_present(*_TEMP_FIELDS)


def _alt_field(series: SeriesBundle):
    return series.first_present(*_ALT_FIELDS)


def atmospheric_density(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Air density from the ideal gas law: rho = P / (R * T).

    Uses the latest valid pressure and temperature.
    """

    name = "atmospheric_density"
    pf = _pressure_field(series)
    tf = _temp_field(series)
    if pf is None or tf is None:
        return ScientificResult.insufficient(name, "need pressure and temperature", category=CATEGORY)
    p = series.latest(pf)
    t = series.latest(tf)
    if p is None or t is None or t.value <= 0:
        return ScientificResult.insufficient(name, "no valid concurrent P/T", category=CATEGORY)
    rho = p.value / (R_SPECIFIC_DRY_AIR * t.value)
    # Propagate uncertainty (independent errors): rho * sqrt((dP/P)^2 + (dT/T)^2).
    unc = None
    if p.uncertainty and t.uncertainty and math.isfinite(p.uncertainty) and math.isfinite(t.uncertainty):
        rel = math.sqrt((p.uncertainty / p.value) ** 2 + (t.uncertainty / t.value) ** 2)
        unc = rho * rel
    return ScientificResult(
        analysis_name=name,
        value=rho,
        unit="kg/m^3",
        method="Ideal gas law rho = P/(R_dry*T), R_dry=287.05 J/(kg*K)",
        result_type=ResultType.DERIVED,
        inputs={"pressure_Pa": p.value, "temperature_K": t.value},
        assumptions=["Dry air", "Ideal gas behaviour", "Well-mixed parcel"],
        uncertainty=unc,
        confidence=0.9,
        limitations=["Humidity not accounted for (dry-air R used)"],
        n_samples=1,
        category=CATEGORY,
    )


def temperature_gradient(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Vertical temperature gradient dT/dz via OLS of temperature on altitude.

    A negative gradient (temperature falling with height) is the normal
    troposphere; a positive gradient indicates an inversion.
    """

    name = "temperature_gradient"
    tf = _temp_field(series)
    af = _alt_field(series)
    if tf is None or af is None:
        return ScientificResult.insufficient(name, "need temperature and altitude", category=CATEGORY)
    # Align by frame: build parallel lists using frames that have both valid.
    z, t = _paired(series, af, tf)
    if len(z) < 3:
        return ScientificResult.insufficient(name, "fewer than 3 paired samples", n_samples=len(z), category=CATEGORY)
    fit = stats.linear_regression(z, t)
    if fit is None:
        return ScientificResult.insufficient(name, "degenerate altitude range", n_samples=len(z), category=CATEGORY)
    slope = fit["slope"]  # K/m
    return ScientificResult(
        analysis_name=name,
        value=slope,
        unit="K/m",
        method="OLS regression of temperature (K) on altitude (m)",
        result_type=ResultType.DERIVED,
        inputs={"n_pairs": fit["n"], "r_squared": fit["r_squared"]},
        assumptions=["Approximately hydrostatic profile over the window"],
        uncertainty=fit["slope_stderr"],
        confidence=max(0.0, min(1.0, fit["r_squared"])),
        limitations=[
            "Local gradient over the sampled altitude band only",
            "Sensor thermal lag can bias steep-ascent gradients",
        ],
        n_samples=fit["n"],
        category=CATEGORY,
    )


def lapse_rate(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Environmental lapse rate = -dT/dz, reported in K/km."""

    grad = temperature_gradient(series)
    name = "lapse_rate"
    if not grad.valid or grad.value is None:
        return ScientificResult.insufficient(name, grad.limitations[0] if grad.limitations else "no gradient", n_samples=grad.n_samples, category=CATEGORY)
    lr_km = -float(grad.value) * 1000.0
    unc_km = grad.uncertainty * 1000.0 if grad.uncertainty is not None and math.isfinite(grad.uncertainty) else None
    return ScientificResult(
        analysis_name=name,
        value=lr_km,
        unit="K/km",
        method="Negative vertical temperature gradient (-dT/dz)",
        result_type=ResultType.DERIVED,
        inputs={"gradient_K_per_m": grad.value},
        assumptions=["ISA standard lapse rate is 6.5 K/km for reference"],
        uncertainty=unc_km,
        confidence=grad.confidence,
        limitations=list(grad.limitations),
        n_samples=grad.n_samples,
        category=CATEGORY,
    )


def temperature_inversion_detection(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Detect a temperature inversion (temperature rising with altitude)."""

    name = "temperature_inversion"
    grad = temperature_gradient(series)
    if not grad.valid or grad.value is None:
        return ScientificResult.insufficient(name, "no reliable gradient", n_samples=grad.n_samples, category=CATEGORY)
    inversion = float(grad.value) > 0.0 and grad.confidence > 0.2
    return ScientificResult(
        analysis_name=name,
        value=bool(inversion),
        unit="",
        method="Sign of dT/dz (positive => inversion) with r^2 gate",
        result_type=ResultType.INFERRED,
        inputs={"gradient_K_per_m": grad.value, "r_squared": grad.confidence},
        assumptions=["Monotonic ascent so altitude proxies height"],
        uncertainty=None,
        confidence=grad.confidence,
        limitations=["Weak/low-r^2 gradients are reported as no inversion"],
        n_samples=grad.n_samples,
        category=CATEGORY,
    )


def pressure_gradient(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Rate of pressure change with time, dP/dt (Pa/s)."""

    name = "pressure_gradient"
    pf = _pressure_field(series)
    if pf is None:
        return ScientificResult.insufficient(name, "no pressure field", category=CATEGORY)
    t, p = series.series(pf)
    if len(t) < 3:
        return ScientificResult.insufficient(name, "fewer than 3 samples", n_samples=len(t), category=CATEGORY)
    fit = stats.linear_regression(t, p)
    if fit is None:
        return ScientificResult.insufficient(name, "degenerate time base", n_samples=len(t), category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=fit["slope"],
        unit="Pa/s",
        method="OLS regression of pressure on time",
        result_type=ResultType.DERIVED,
        inputs={"n": fit["n"], "r_squared": fit["r_squared"]},
        assumptions=["Approximately linear over the window"],
        uncertainty=fit["slope_stderr"],
        confidence=max(0.0, min(1.0, fit["r_squared"])),
        limitations=["Window-local rate; not a synoptic tendency"],
        n_samples=fit["n"],
        category=CATEGORY,
    )


def hypsometric_altitude(series: SeriesBundle, sea_level_pressure: float = P0_SEA_LEVEL, **_: Any) -> ScientificResult:
    """Barometric altitude from the hypsometric equation (ISA troposphere)."""

    name = "hypsometric_altitude"
    pf = _pressure_field(series)
    tf = _temp_field(series)
    if pf is None:
        return ScientificResult.insufficient(name, "no pressure field", category=CATEGORY)
    p = series.latest(pf)
    if p is None or p.value <= 0:
        return ScientificResult.insufficient(name, "no valid pressure", category=CATEGORY)
    t = series.latest(tf) if tf else None
    t_ref = t.value if (t and t.value > 0) else T0_SEA_LEVEL
    # h = (T_ref / L) * (1 - (P/P0)^(R*L/g))
    exponent = (R_SPECIFIC_DRY_AIR * STANDARD_LAPSE_RATE) / G0
    h = (t_ref / STANDARD_LAPSE_RATE) * (1.0 - (p.value / sea_level_pressure) ** exponent)
    return ScientificResult(
        analysis_name=name,
        value=h,
        unit="m",
        method="Hypsometric equation with ISA lapse rate 6.5 K/km",
        result_type=ResultType.MODELLED,
        inputs={"pressure_Pa": p.value, "sea_level_pressure_Pa": sea_level_pressure, "T_ref_K": t_ref},
        assumptions=[
            "ISA standard atmosphere below 11 km",
            f"Sea-level reference pressure = {sea_level_pressure:.0f} Pa",
        ],
        uncertainty=None,
        confidence=0.7,
        limitations=[
            "Sensitive to the assumed sea-level pressure",
            "Valid only in the troposphere (<11 km)",
        ],
        n_samples=1,
        category=CATEGORY,
    )


def dew_point(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Dew point via the Magnus-Tetens approximation (needs humidity)."""

    name = "dew_point"
    tf = _temp_field(series)
    hf = series.first_present("humidity", "bme688_humidity", "relative_humidity")
    if tf is None or hf is None:
        return ScientificResult.insufficient(name, "need temperature and relative humidity", category=CATEGORY)
    t = series.latest(tf)
    h = series.latest(hf)
    if t is None or h is None or h.value <= 0 or h.value > 100:
        return ScientificResult.insufficient(name, "invalid humidity or temperature", category=CATEGORY)
    tc = t.value - 273.15  # to Celsius
    a, b = 17.62, 243.12
    gamma = math.log(h.value / 100.0) + (a * tc) / (b + tc)
    td_c = (b * gamma) / (a - gamma)
    return ScientificResult(
        analysis_name=name,
        value=td_c + 273.15,
        unit="K",
        method="Magnus-Tetens (a=17.62, b=243.12 C)",
        result_type=ResultType.DERIVED,
        inputs={"temperature_C": tc, "relative_humidity_pct": h.value},
        assumptions=["Magnus coefficients valid roughly -45..60 C"],
        uncertainty=None,
        confidence=0.85,
        limitations=["Accuracy degrades at very low humidity"],
        n_samples=1,
        category=CATEGORY,
    )


def atmospheric_stability_index(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Compare the environmental lapse rate to the dry adiabatic lapse rate.

    DALR ~= 9.8 K/km. If the environmental lapse rate exceeds the DALR the
    layer is (absolutely) unstable; if below, it is stable.
    """

    name = "atmospheric_stability"
    lr = lapse_rate(series)
    if not lr.valid or lr.value is None:
        return ScientificResult.insufficient(name, "no lapse rate", n_samples=lr.n_samples, category=CATEGORY)
    dalr = 9.8  # K/km
    env = float(lr.value)
    if env > dalr:
        classification = "unstable"
    elif env < 0:
        classification = "inversion (very stable)"
    else:
        classification = "stable"
    return ScientificResult(
        analysis_name=name,
        value=classification,
        unit="",
        method="Environmental lapse rate vs dry adiabatic lapse rate (9.8 K/km)",
        result_type=ResultType.INFERRED,
        inputs={"env_lapse_rate_K_per_km": env, "dry_adiabatic_K_per_km": dalr},
        assumptions=["Dry parcel (no latent heat release considered)"],
        uncertainty=None,
        confidence=lr.confidence,
        limitations=["Moist convection not modelled; classification is indicative"],
        n_samples=lr.n_samples,
        category=CATEGORY,
    )


def boundary_layer_height(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Estimate the boundary-layer top as the altitude of the strongest
    positive curvature (kink) in the potential-temperature-like profile.

    This is a heuristic estimate; it is reported as INFERRED with clear
    limitations, never as a precise measured height.
    """

    name = "boundary_layer_height"
    tf = _temp_field(series)
    af = _alt_field(series)
    if tf is None or af is None:
        return ScientificResult.insufficient(name, "need temperature and altitude", category=CATEGORY)
    z, t = _paired(series, af, tf)
    if len(z) < 6:
        return ScientificResult.insufficient(name, "need >=6 paired samples", n_samples=len(z), category=CATEGORY)
    # Sort by altitude and find the largest jump in dT/dz (inversion base).
    order = sorted(range(len(z)), key=lambda i: z[i])
    zs = [z[i] for i in order]
    tts = [t[i] for i in order]
    best_z = None
    best_d = -1e9
    for i in range(1, len(zs)):
        dz = zs[i] - zs[i - 1]
        if dz <= 0:
            continue
        d = (tts[i] - tts[i - 1]) / dz
        if d > best_d:
            best_d = d
            best_z = zs[i]
    if best_z is None or best_d <= 0:
        return ScientificResult.insufficient(name, "no positive gradient / inversion found", n_samples=len(z), category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=best_z,
        unit="m",
        method="Altitude of maximum positive dT/dz (inversion base heuristic)",
        result_type=ResultType.INFERRED,
        inputs={"max_gradient_K_per_m": best_d, "n_pairs": len(z)},
        assumptions=["Boundary layer capped by a temperature inversion"],
        uncertainty=None,
        confidence=0.4,
        limitations=[
            "Heuristic; sonde-grade BLH needs humidity & wind profiles",
            "Sensitive to vertical sampling resolution",
        ],
        n_samples=len(z),
        category=CATEGORY,
    )


def vertical_profile(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Return a compact vertical profile of temperature vs altitude."""

    name = "vertical_profile"
    tf = _temp_field(series)
    af = _alt_field(series)
    if tf is None or af is None:
        return ScientificResult.insufficient(name, "need temperature and altitude", category=CATEGORY)
    z, t = _paired(series, af, tf)
    if len(z) < 3:
        return ScientificResult.insufficient(name, "fewer than 3 paired samples", n_samples=len(z), category=CATEGORY)
    order = sorted(range(len(z)), key=lambda i: z[i])
    profile = [{"altitude_m": z[i], "temperature_K": t[i]} for i in order]
    return ScientificResult(
        analysis_name=name,
        value=profile,
        unit="",
        method="Altitude-sorted (T, z) pairs",
        result_type=ResultType.OBSERVATION,
        inputs={"n_points": len(profile)},
        assumptions=[],
        uncertainty=None,
        confidence=0.8,
        limitations=["Raw sampled profile; not interpolated to standard levels"],
        n_samples=len(profile),
        category=CATEGORY,
    )


def _paired(series: SeriesBundle, field_x: str, field_y: str):
    """Return parallel lists of (x, y) for frames where both are valid."""

    xs, ys = [], []
    for f in series._frames:  # internal access is fine within package
        mx = f.measurements.get(field_x)
        my = f.measurements.get(field_y)
        if mx is None or my is None or not mx.valid or not my.valid:
            continue
        xs.append(float(mx.value))
        ys.append(float(my.value))
    return xs, ys


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("atmospheric_density", CATEGORY, atmospheric_density,
                 "Air density from ideal gas law", ["pressure", "temperature"])
    reg.register("temperature_gradient", CATEGORY, temperature_gradient,
                 "Vertical temperature gradient dT/dz", ["temperature", "altitude"], 3)
    reg.register("lapse_rate", CATEGORY, lapse_rate,
                 "Environmental lapse rate (K/km)", ["temperature", "altitude"], 3)
    reg.register("temperature_inversion", CATEGORY, temperature_inversion_detection,
                 "Detect temperature inversion", ["temperature", "altitude"], 3)
    reg.register("pressure_gradient", CATEGORY, pressure_gradient,
                 "Pressure tendency dP/dt", ["pressure"], 3)
    reg.register("hypsometric_altitude", CATEGORY, hypsometric_altitude,
                 "Barometric altitude (hypsometric)", ["pressure"])
    reg.register("dew_point", CATEGORY, dew_point,
                 "Dew point (Magnus-Tetens)", ["temperature", "humidity"])
    reg.register("atmospheric_stability", CATEGORY, atmospheric_stability_index,
                 "Stability vs dry adiabatic lapse rate", ["temperature", "altitude"], 3)
    reg.register("boundary_layer_height", CATEGORY, boundary_layer_height,
                 "Boundary-layer height heuristic", ["temperature", "altitude"], 6)
    reg.register("vertical_profile", CATEGORY, vertical_profile,
                 "Temperature vertical profile", ["temperature", "altitude"], 3)
