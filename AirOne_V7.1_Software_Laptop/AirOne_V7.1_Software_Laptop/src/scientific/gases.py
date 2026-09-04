"""Air-quality / gas-sensor analyses (SGP41 VOC/NOx, SEN0463, etc.).

Gas metal-oxide sensors are strongly temperature/humidity dependent. Where a
correction is applied it is flagged as an assumption. Indices produced here are
*proxies*, clearly labelled MODELLED/INFERRED, not regulatory AQI values.
"""
from __future__ import annotations

from typing import Any

from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "gases"

_VOC_FIELDS = ("voc_index", "sgp41_voc", "voc")
_NOX_FIELDS = ("nox_index", "sgp41_nox", "nox")
_CO2_FIELDS = ("co2", "eco2", "co2_ppm")


def voc_profile(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Summarise the VOC index over the window (mean, trend)."""

    name = "voc_profile"
    vf = series.first_present(*_VOC_FIELDS)
    if vf is None:
        return ScientificResult.insufficient(name, "no VOC field", category=CATEGORY)
    t, vals = series.series(vf)
    ms = stats.mean_std(vals)
    if ms is None:
        return ScientificResult.insufficient(name, "fewer than 2 samples", n_samples=len(vals), category=CATEGORY)
    mean, std = ms
    fit = stats.linear_regression(t, vals)
    trend = fit["slope"] if fit else None
    return ScientificResult(
        analysis_name=name,
        value={"mean": mean, "std": std, "trend_per_s": trend},
        unit="index",
        method="Mean/std of VOC index; OLS trend vs time",
        result_type=ResultType.MEASURED,
        inputs={"n": len(vals)},
        assumptions=["SGP41 VOC index is a unitless relative scale (1..500)"],
        uncertainty=std,
        confidence=0.8,
        limitations=["Relative index, not an absolute concentration"],
        n_samples=len(vals),
        category=CATEGORY,
    )


def air_quality_proxy(series: SeriesBundle, **_: Any) -> ScientificResult:
    """A simple 0..100 air-quality proxy from VOC and NOx indices.

    This is explicitly a PROXY: it blends the relative sensor indices and is
    not a regulatory AQI. Reported as MODELLED with clear limitations.
    """

    name = "air_quality_proxy"
    vf = series.first_present(*_VOC_FIELDS)
    nf = series.first_present(*_NOX_FIELDS)
    if vf is None and nf is None:
        return ScientificResult.insufficient(name, "need VOC or NOx index", category=CATEGORY)
    parts = {}
    score_terms = []
    if vf is not None:
        mv = series.latest(vf)
        if mv is not None:
            parts["voc_index"] = mv.value
            # VOC index ~100 is baseline; higher is worse.
            score_terms.append(min(100.0, max(0.0, (mv.value - 100.0) / 4.0)))
    if nf is not None:
        mn = series.latest(nf)
        if mn is not None:
            parts["nox_index"] = mn.value
            score_terms.append(min(100.0, max(0.0, (mn.value - 1.0) * 5.0)))
    if not score_terms:
        return ScientificResult.insufficient(name, "no valid gas readings", category=CATEGORY)
    score = sum(score_terms) / len(score_terms)
    return ScientificResult(
        analysis_name=name,
        value=score,
        unit="proxy 0-100",
        method="Blended normalised VOC/NOx index (0 good .. 100 poor)",
        result_type=ResultType.MODELLED,
        inputs=parts,
        assumptions=["VOC baseline ~100", "NOx baseline ~1"],
        uncertainty=None,
        confidence=0.4,
        limitations=[
            "NOT a regulatory AQI",
            "Uncompensated for temperature/humidity",
        ],
        n_samples=1,
        category=CATEGORY,
    )


def plume_detection(series: SeriesBundle, sigma: float = 3.0, **_: Any) -> ScientificResult:
    """Detect a gas plume as a VOC excursion above mean + sigma*std."""

    name = "plume_detection"
    vf = series.first_present(*_VOC_FIELDS)
    if vf is None:
        return ScientificResult.insufficient(name, "no VOC field", category=CATEGORY)
    t, vals = series.series(vf)
    ms = stats.mean_std(vals)
    if ms is None or len(vals) < 5:
        return ScientificResult.insufficient(name, "need >=5 samples", n_samples=len(vals), category=CATEGORY)
    mean, std = ms
    threshold = mean + sigma * std
    hits = [{"t_s": t[i], "voc": vals[i]} for i in range(len(vals)) if vals[i] > threshold]
    return ScientificResult(
        analysis_name=name,
        value=len(hits),
        unit="events",
        method=f"VOC excursion above mean + {sigma}*std",
        result_type=ResultType.OBSERVATION,
        inputs={"mean": mean, "std": std, "threshold": threshold, "hits": hits},
        assumptions=["Baseline is approximately stationary"],
        uncertainty=None,
        confidence=0.6,
        limitations=["Cannot identify the specific compound"],
        n_samples=len(vals),
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("voc_profile", CATEGORY, voc_profile, "VOC index summary", [], 2)
    reg.register("air_quality_proxy", CATEGORY, air_quality_proxy, "Air-quality proxy 0-100", [], 1)
    reg.register("plume_detection", CATEGORY, plume_detection, "VOC plume detection", [], 5)
