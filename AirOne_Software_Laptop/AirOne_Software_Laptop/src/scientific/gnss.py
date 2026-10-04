"""GNSS-derived analyses (MAX-M10S): distance, speed, bearing, ascent/descent.

Distances use the haversine formula on a spherical Earth (R=6371 km). This is
accurate to ~0.5% versus the WGS-84 ellipsoid, which is noted as a limitation.
"""
from __future__ import annotations

import math
from typing import Any, List, Tuple

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "gnss"

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points (degrees)."""

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees 0..360."""

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _track(series: SeriesBundle) -> List[Tuple[float, float, float]]:
    """Return [(t_s, lat, lon), ...] for valid concurrent fixes."""

    latf = series.first_present("gnss_lat", "latitude", "lat")
    lonf = series.first_present("gnss_lon", "longitude", "lon")
    if latf is None or lonf is None:
        return []
    out = []
    for f in series._frames:
        mla = f.measurements.get(latf)
        mlo = f.measurements.get(lonf)
        if not (mla and mlo) or not (mla.valid and mlo.valid):
            continue
        t = (f.timestamp - series._t0).total_seconds() if series._t0 else 0.0
        out.append((t, float(mla.value), float(mlo.value)))
    return out


def ground_track_distance(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Total horizontal ground-track distance flown."""

    name = "ground_track_distance"
    track = _track(series)
    if len(track) < 2:
        return ScientificResult.insufficient(name, "need >=2 GNSS fixes", n_samples=len(track), category=CATEGORY)
    total = 0.0
    for i in range(1, len(track)):
        total += haversine_m(track[i - 1][1], track[i - 1][2], track[i][1], track[i][2])
    return ScientificResult(
        analysis_name=name,
        value=total,
        unit="m",
        method="Sum of haversine segment distances (R=6371 km sphere)",
        result_type=ResultType.DERIVED,
        inputs={"n_fixes": len(track)},
        assumptions=["Spherical Earth"],
        uncertainty=None,
        confidence=0.8,
        limitations=["~0.5% vs WGS-84 ellipsoid", "GNSS jitter inflates short segments"],
        n_samples=len(track),
        category=CATEGORY,
    )


def ground_speed(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Mean horizontal ground speed from consecutive fixes."""

    name = "ground_speed"
    track = _track(series)
    if len(track) < 2:
        return ScientificResult.insufficient(name, "need >=2 GNSS fixes", n_samples=len(track), category=CATEGORY)
    speeds = []
    for i in range(1, len(track)):
        dt = track[i][0] - track[i - 1][0]
        if dt <= 0:
            continue
        d = haversine_m(track[i - 1][1], track[i - 1][2], track[i][1], track[i][2])
        speeds.append(d / dt)
    ms = stats.mean_std(speeds)
    if ms is None:
        if not speeds:
            return ScientificResult.insufficient(name, "no positive time deltas", n_samples=len(track), category=CATEGORY)
        mean, std = speeds[0], 0.0
    else:
        mean, std = ms
    return ScientificResult(
        analysis_name=name,
        value=mean,
        unit="m/s",
        method="Mean of per-segment haversine distance / time delta",
        result_type=ResultType.DERIVED,
        inputs={"n_segments": len(speeds)},
        assumptions=["Spherical Earth", "Straight-line between fixes"],
        uncertainty=std,
        confidence=0.7,
        limitations=["GNSS position noise inflates speed on short segments"],
        n_samples=len(speeds),
        category=CATEGORY,
    )


def vertical_speed(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Vertical speed from GNSS altitude vs time (OLS slope)."""

    name = "vertical_speed"
    af = series.first_present("gnss_altitude", "altitude", "fused_altitude")
    if af is None:
        return ScientificResult.insufficient(name, "no altitude field", category=CATEGORY)
    t, z = series.series(af)
    fit = stats.linear_regression(t, z)
    if fit is None:
        return ScientificResult.insufficient(name, "fewer than 3 samples", n_samples=len(t), category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=fit["slope"],
        unit="m/s",
        method="OLS slope of altitude vs time",
        result_type=ResultType.DERIVED,
        inputs={"n": fit["n"], "r_squared": fit["r_squared"]},
        assumptions=["Approximately constant vertical rate over the window"],
        uncertainty=fit["slope_stderr"],
        confidence=max(0.0, min(1.0, fit["r_squared"])),
        limitations=["Window-mean rate; not instantaneous"],
        n_samples=fit["n"],
        category=CATEGORY,
    )


def displacement_from_start(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Straight-line distance and bearing from the first to the last fix."""

    name = "displacement_from_start"
    track = _track(series)
    if len(track) < 2:
        return ScientificResult.insufficient(name, "need >=2 GNSS fixes", n_samples=len(track), category=CATEGORY)
    _, la0, lo0 = track[0]
    _, la1, lo1 = track[-1]
    dist = haversine_m(la0, lo0, la1, lo1)
    brg = bearing_deg(la0, lo0, la1, lo1)
    return ScientificResult(
        analysis_name=name,
        value={"distance_m": dist, "bearing_deg": brg},
        unit="m",
        method="Haversine + initial bearing between first and last fix",
        result_type=ResultType.DERIVED,
        inputs={"start": [la0, lo0], "end": [la1, lo1]},
        assumptions=["Spherical Earth"],
        uncertainty=None,
        confidence=0.8,
        limitations=["Straight-line displacement, not path length"],
        n_samples=len(track),
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("ground_track_distance", CATEGORY, ground_track_distance, "Total ground track distance", [], 2)
    reg.register("ground_speed", CATEGORY, ground_speed, "Mean ground speed", [], 2)
    reg.register("vertical_speed", CATEGORY, vertical_speed, "Vertical speed from altitude", [], 3)
    reg.register("displacement_from_start", CATEGORY, displacement_from_start, "Net displacement", [], 2)
