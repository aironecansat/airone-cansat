"""Ionising radiation analyses (Geiger / scintillator counts).

Counting statistics follow a Poisson distribution, so the standard uncertainty
on N counts is sqrt(N). This is applied throughout. Correlations between
radiation and other variables (e.g. altitude) are reported as CORRELATION and
therefore automatically carry the "correlation does not imply causation"
caveat.
"""
from __future__ import annotations

import math
from typing import Any

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "radiation"

_COUNT_FIELDS = ("radiation_cpm", "geiger_cpm", "counts_per_minute", "radiation_counts")
_DOSE_FIELDS = ("dose_rate", "radiation_dose", "usv_h")


def _count_field(series: SeriesBundle):
    return series.first_present(*_COUNT_FIELDS)


def dose_rate(series: SeriesBundle, conversion_factor: float = 0.0057, **_: Any) -> ScientificResult:
    """Estimate dose rate (uSv/h) from counts per minute.

    The conversion factor is tube-specific; the default (0.0057 uSv/h per CPM)
    corresponds to a common SBM-20 tube for Cs-137. It is surfaced as an
    explicit assumption because it dominates the systematic uncertainty.
    """

    name = "dose_rate"
    df = series.first_present(*_DOSE_FIELDS)
    if df is not None:
        m = series.latest(df)
        if m is not None:
            return ScientificResult(
                analysis_name=name, value=m.value, unit=m.unit or "uSv/h",
                method="Direct dose-rate telemetry",
                result_type=ResultType.MEASURED,
                inputs={"field": df}, assumptions=["Sensor pre-calibrated"],
                uncertainty=m.uncertainty if math.isfinite(m.uncertainty) else None,
                confidence=0.9, limitations=[], n_samples=1, category=CATEGORY,
            )
    cf = _count_field(series)
    if cf is None:
        return ScientificResult.insufficient(name, "no radiation count field", category=CATEGORY)
    m = series.latest(cf)
    if m is None or m.value < 0:
        return ScientificResult.insufficient(name, "no valid CPM value", category=CATEGORY)
    cpm = m.value
    dose = cpm * conversion_factor
    # Poisson uncertainty on the count -> propagate through the linear factor.
    counts_unc = math.sqrt(cpm) if cpm > 0 else 0.0
    dose_unc = counts_unc * conversion_factor
    return ScientificResult(
        analysis_name=name,
        value=dose,
        unit="uSv/h",
        method="Dose = CPM * tube_factor (Poisson counting statistics)",
        result_type=ResultType.MODELLED,
        inputs={"cpm": cpm, "conversion_factor_uSv_per_CPM": conversion_factor},
        assumptions=[
            f"Tube conversion factor {conversion_factor} uSv/h per CPM (SBM-20/Cs-137)",
            "Isotropic field, Cs-137-like spectrum",
        ],
        uncertainty=dose_unc,
        confidence=0.6,
        limitations=[
            "Conversion factor is tube- and spectrum-specific",
            "Not a calibrated dosimeter reading",
        ],
        n_samples=1,
        category=CATEGORY,
    )


def mean_count_rate(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Mean counts per minute over the window with Poisson-based uncertainty."""

    name = "mean_count_rate"
    cf = _count_field(series)
    if cf is None:
        return ScientificResult.insufficient(name, "no radiation count field", category=CATEGORY)
    vals = series.values(cf)
    if len(vals) < 1:
        return ScientificResult.insufficient(name, "no valid samples", category=CATEGORY)
    total = sum(vals)
    mean = total / len(vals)
    # Uncertainty on the mean count via Poisson: sqrt(sum)/N.
    unc = (math.sqrt(total) / len(vals)) if total > 0 else 0.0
    return ScientificResult(
        analysis_name=name,
        value=mean,
        unit="CPM",
        method="Arithmetic mean of CPM; uncertainty = sqrt(total_counts)/N",
        result_type=ResultType.MEASURED,
        inputs={"n": len(vals), "total_counts_proxy": total},
        assumptions=["Counts are Poisson-distributed", "Stationary field over window"],
        uncertainty=unc,
        confidence=0.85,
        limitations=["Assumes each sample integrates the same live-time"],
        n_samples=len(vals),
        category=CATEGORY,
    )


def burst_detection(series: SeriesBundle, sigma: float = 3.0, **_: Any) -> ScientificResult:
    """Detect count-rate bursts exceeding mean + sigma*sqrt(mean) (Poisson)."""

    name = "radiation_burst"
    cf = _count_field(series)
    if cf is None:
        return ScientificResult.insufficient(name, "no radiation count field", category=CATEGORY)
    t, vals = series.series(cf)
    if len(vals) < 5:
        return ScientificResult.insufficient(name, "need >=5 samples", n_samples=len(vals), category=CATEGORY)
    mean = sum(vals) / len(vals)
    threshold = mean + sigma * math.sqrt(mean) if mean > 0 else float("inf")
    bursts = [{"t_s": t[i], "cpm": vals[i]} for i in range(len(vals)) if vals[i] > threshold]
    return ScientificResult(
        analysis_name=name,
        value=len(bursts),
        unit="events",
        method=f"Poisson threshold mean + {sigma}*sqrt(mean)",
        result_type=ResultType.OBSERVATION,
        inputs={"mean_cpm": mean, "threshold_cpm": threshold, "bursts": bursts},
        assumptions=["Poisson background", "Bursts are short relative to sampling"],
        uncertainty=None,
        confidence=0.7,
        limitations=["Sensitive to the chosen sigma; short bursts may be missed"],
        n_samples=len(vals),
        category=CATEGORY,
    )


def altitude_correlation(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Correlation between radiation count rate and altitude.

    Reported strictly as a CORRELATION (never causal). Physically, cosmic-ray
    flux tends to rise with altitude, but this analysis only measures
    association within the flight window.
    """

    name = "radiation_altitude_correlation"
    cf = _count_field(series)
    af = series.first_present("altitude", "fused_altitude", "gnss_altitude")
    if cf is None or af is None:
        return ScientificResult.insufficient(name, "need radiation and altitude", category=CATEGORY)
    xs, ys = [], []
    for f in series._frames:
        mc = f.measurements.get(cf)
        ma = f.measurements.get(af)
        if mc is None or ma is None or not mc.valid or not ma.valid:
            continue
        xs.append(float(ma.value))
        ys.append(float(mc.value))
    pr = stats.pearson_r(xs, ys)
    if pr is None:
        return ScientificResult.insufficient(name, "fewer than 3 paired samples", n_samples=len(xs), category=CATEGORY)
    r, n = pr
    return ScientificResult(
        analysis_name=name,
        value=r,
        unit="",
        method="Pearson correlation of CPM vs altitude",
        result_type=ResultType.CORRELATION,
        inputs={"n_pairs": n},
        assumptions=["Linear association model"],
        uncertainty=None,
        confidence=min(1.0, abs(r)),
        limitations=["Association only over the flight altitude band"],
        n_samples=n,
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("dose_rate", CATEGORY, dose_rate, "Dose rate from CPM", [], 1)
    reg.register("mean_count_rate", CATEGORY, mean_count_rate, "Mean CPM with Poisson error", [], 1)
    reg.register("radiation_burst", CATEGORY, burst_detection, "Detect count-rate bursts", [], 5)
    reg.register("radiation_altitude_correlation", CATEGORY, altitude_correlation,
                 "Radiation vs altitude correlation", [], 3)
