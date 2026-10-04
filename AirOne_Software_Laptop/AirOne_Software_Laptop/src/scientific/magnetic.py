"""Magnetometer analyses (MMC5603 3-axis).

Vector magnitude, heading, inclination and anomaly detection. Headings are
geometric (magnetic), with declination correction noted as an assumption.
"""
from __future__ import annotations

import math
from typing import Any

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "magnetic"


def _axes(series: SeriesBundle):
    xf = series.first_present("mag_x", "magnetometer_x", "mx")
    yf = series.first_present("mag_y", "magnetometer_y", "my")
    zf = series.first_present("mag_z", "magnetometer_z", "mz")
    return xf, yf, zf


def field_magnitude(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Total magnetic field magnitude sqrt(x^2+y^2+z^2)."""

    name = "magnetic_field_magnitude"
    xf, yf, zf = _axes(series)
    if not (xf and yf and zf):
        return ScientificResult.insufficient(name, "need mag_x/y/z", category=CATEGORY)
    mx, my, mz = series.latest(xf), series.latest(yf), series.latest(zf)
    if not (mx and my and mz):
        return ScientificResult.insufficient(name, "no valid 3-axis sample", category=CATEGORY)
    mag = math.sqrt(mx.value ** 2 + my.value ** 2 + mz.value ** 2)
    return ScientificResult(
        analysis_name=name,
        value=mag,
        unit=mx.unit or "uT",
        method="Euclidean norm of 3-axis magnetometer",
        result_type=ResultType.DERIVED,
        inputs={"x": mx.value, "y": my.value, "z": mz.value},
        assumptions=["Axes orthogonal and equally scaled (hard-iron uncorrected)"],
        uncertainty=None,
        confidence=0.85,
        limitations=["Hard/soft-iron distortion from the CanSat body not removed"],
        n_samples=1,
        category=CATEGORY,
    )


def heading(series: SeriesBundle, declination_deg: float = 0.0, **_: Any) -> ScientificResult:
    """Magnetic heading from the horizontal components: atan2(y, x)."""

    name = "magnetic_heading"
    xf, yf, _zf = _axes(series)
    if not (xf and yf):
        return ScientificResult.insufficient(name, "need mag_x and mag_y", category=CATEGORY)
    mx, my = series.latest(xf), series.latest(yf)
    if not (mx and my):
        return ScientificResult.insufficient(name, "no valid horizontal sample", category=CATEGORY)
    hdg = math.degrees(math.atan2(my.value, mx.value))
    hdg = (hdg + declination_deg) % 360.0
    return ScientificResult(
        analysis_name=name,
        value=hdg,
        unit="deg",
        method="atan2(y, x) + declination, normalised to 0..360",
        result_type=ResultType.DERIVED,
        inputs={"x": mx.value, "y": my.value, "declination_deg": declination_deg},
        assumptions=["Sensor is level (no tilt compensation)", "Declination supplied by caller"],
        uncertainty=None,
        confidence=0.6,
        limitations=["No tilt compensation; heading degrades when not level"],
        n_samples=1,
        category=CATEGORY,
    )


def inclination(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Magnetic inclination (dip) angle from horizontal and vertical parts."""

    name = "magnetic_inclination"
    xf, yf, zf = _axes(series)
    if not (xf and yf and zf):
        return ScientificResult.insufficient(name, "need mag_x/y/z", category=CATEGORY)
    mx, my, mz = series.latest(xf), series.latest(yf), series.latest(zf)
    if not (mx and my and mz):
        return ScientificResult.insufficient(name, "no valid 3-axis sample", category=CATEGORY)
    horiz = math.sqrt(mx.value ** 2 + my.value ** 2)
    if horiz == 0:
        return ScientificResult.insufficient(name, "zero horizontal component", category=CATEGORY)
    inc = math.degrees(math.atan2(mz.value, horiz))
    return ScientificResult(
        analysis_name=name,
        value=inc,
        unit="deg",
        method="atan2(z, sqrt(x^2+y^2))",
        result_type=ResultType.DERIVED,
        inputs={"horizontal": horiz, "z": mz.value},
        assumptions=["Sensor level; body not compensated"],
        uncertainty=None,
        confidence=0.5,
        limitations=["Attitude of the CanSat not accounted for"],
        n_samples=1,
        category=CATEGORY,
    )


def anomaly_detection(series: SeriesBundle, sigma: float = 3.0, **_: Any) -> ScientificResult:
    """Detect magnetic anomalies as magnitude excursions beyond sigma*std."""

    name = "magnetic_anomaly"
    xf, yf, zf = _axes(series)
    if not (xf and yf and zf):
        return ScientificResult.insufficient(name, "need mag_x/y/z", category=CATEGORY)
    mags = []
    ts = []
    for f in series._frames:
        mx = f.measurements.get(xf); my = f.measurements.get(yf); mz = f.measurements.get(zf)
        if not (mx and my and mz) or not (mx.valid and my.valid and mz.valid):
            continue
        mags.append(math.sqrt(mx.value ** 2 + my.value ** 2 + mz.value ** 2))
        ts.append((f.timestamp - series._t0).total_seconds() if series._t0 else 0.0)
    ms = stats.mean_std(mags)
    if ms is None or len(mags) < 5:
        return ScientificResult.insufficient(name, "need >=5 samples", n_samples=len(mags), category=CATEGORY)
    mean, std = ms
    hits = [{"t_s": ts[i], "magnitude": mags[i]} for i in range(len(mags)) if abs(mags[i] - mean) > sigma * std]
    return ScientificResult(
        analysis_name=name,
        value=len(hits),
        unit="events",
        method=f"|magnitude - mean| > {sigma}*std",
        result_type=ResultType.OBSERVATION,
        inputs={"mean": mean, "std": std, "hits": hits},
        assumptions=["Stationary background field over the window"],
        uncertainty=None,
        confidence=0.5,
        limitations=["CanSat electronics can induce local anomalies"],
        n_samples=len(mags),
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("magnetic_field_magnitude", CATEGORY, field_magnitude, "Total field magnitude", [], 1)
    reg.register("magnetic_heading", CATEGORY, heading, "Magnetic heading", [], 1)
    reg.register("magnetic_inclination", CATEGORY, inclination, "Magnetic dip angle", [], 1)
    reg.register("magnetic_anomaly", CATEGORY, anomaly_detection, "Magnetic anomaly detection", [], 5)
