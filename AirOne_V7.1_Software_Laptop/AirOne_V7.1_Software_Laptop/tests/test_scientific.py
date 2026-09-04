"""Scientific analysis tests using known numeric vectors.

These verify the *maths*, not merely that code runs: temperature gradient
against a synthetic linear profile, haversine against a reference distance,
dew point against a textbook value, Poisson uncertainty = sqrt(N), atmospheric
density against the ideal gas law, and the honesty invariants (correlation
carries the causation caveat; insufficient data returns an invalid result).
"""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from src.core.models import (
    DataSource,
    Measurement,
    QualityState,
    TelemetryFrame,
    utcnow,
)
from src.scientific import ResultType, SeriesBundle, build_default_registry
from src.scientific import gnss as gnss_mod


REG = build_default_registry()


def _frame(t0, dt_s, **fields):
    ts = t0 + timedelta(seconds=dt_s)
    fr = TelemetryFrame(timestamp=ts)
    for name, value in fields.items():
        fr.add(Measurement(
            value=value, unit="", timestamp=ts, sensor_id=name,
            quality=QualityState.VALID, valid=True, source=DataSource.CALIBRATED,
            field_name=name,
        ))
    return fr


def test_temperature_gradient_recovers_known_slope():
    """A synthetic -0.008 K/m profile must be recovered within tolerance."""

    t0 = utcnow()
    frames = []
    for i in range(20):
        alt = 100.0 * i
        temp = 300.0 - 0.008 * alt  # exact linear lapse
        frames.append(_frame(t0, i, altitude=alt, temperature=temp))
    res = REG.run("temperature_gradient", SeriesBundle(frames))
    assert res.valid
    assert res.value == pytest.approx(-0.008, abs=1e-6)
    assert res.result_type == ResultType.DERIVED


def test_atmospheric_density_ideal_gas():
    t0 = utcnow()
    # P=101325 Pa, T=288.15 K -> rho = P/(287.05*T) ~ 1.225 kg/m^3
    frames = [_frame(t0, 0, pressure=101325.0, temperature=288.15)]
    res = REG.run("atmospheric_density", SeriesBundle(frames))
    assert res.valid
    assert res.value == pytest.approx(1.225, abs=2e-3)


def test_haversine_reference_distance():
    # ~1 degree of latitude is ~111.19 km on a 6371 km sphere.
    d = gnss_mod.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert d == pytest.approx(111195.0, rel=1e-3)


def test_dew_point_textbook_value():
    t0 = utcnow()
    # T=25 C (298.15 K), RH=50% -> dew point ~= 13.85 C
    frames = [_frame(t0, 0, temperature=298.15, humidity=50.0)]
    res = REG.run("dew_point", SeriesBundle(frames))
    assert res.valid
    assert (res.value - 273.15) == pytest.approx(13.85, abs=0.3)


def test_poisson_uncertainty_is_sqrt_n():
    t0 = utcnow()
    # Constant 100 CPM over the window -> mean 100, uncertainty sqrt(total)/N.
    frames = [_frame(t0, i, radiation_cpm=100.0) for i in range(9)]
    res = REG.run("mean_count_rate", SeriesBundle(frames))
    assert res.valid
    assert res.value == pytest.approx(100.0)
    # total=900, N=9 -> sqrt(900)/9 = 30/9 = 3.333
    assert res.uncertainty == pytest.approx(30.0 / 9.0, abs=1e-6)


def test_correlation_carries_causation_caveat():
    t0 = utcnow()
    frames = []
    for i in range(15):
        alt = 100.0 * i
        frames.append(_frame(t0, i, altitude=alt, radiation_cpm=20.0 + 0.01 * alt))
    res = REG.run("radiation_altitude_correlation", SeriesBundle(frames))
    assert res.valid
    assert res.result_type == ResultType.CORRELATION
    assert any("causation" in lim.lower() for lim in res.limitations)


def test_insufficient_data_returns_invalid_not_fabricated():
    t0 = utcnow()
    frames = [_frame(t0, 0, temperature=290.0, altitude=0.0)]  # single sample
    res = REG.run("temperature_gradient", SeriesBundle(frames))
    assert not res.valid
    assert res.value is None
    assert res.limitations


def test_ground_track_distance_two_points():
    t0 = utcnow()
    f1 = _frame(t0, 0, gnss_lat=0.0, gnss_lon=0.0)
    f2 = _frame(t0, 1, gnss_lat=0.0, gnss_lon=1.0)
    res = REG.run("ground_track_distance", SeriesBundle([f1, f2]))
    assert res.valid
    assert res.value == pytest.approx(111195.0, rel=1e-3)


def test_registry_has_expected_categories():
    cats = REG.categories()
    for expected in ("atmospheric", "radiation", "gnss", "descent", "composite"):
        assert expected in cats
    assert len(REG.names()) >= 30


def test_every_analysis_handles_empty_series():
    """No analysis may raise on an empty window; all must return invalid."""

    empty = SeriesBundle([])
    for name in REG.names():
        res = REG.run(name, empty)
        assert res.analysis_name == name
        assert not res.valid  # empty window -> nothing valid
