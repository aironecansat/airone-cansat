"""Air-quality / gas-sensor analyses (SGP41 VOC/NOx, SEN0463, etc.).

Gas metal-oxide sensors are strongly temperature/humidity dependent. Where a
correction is applied it is flagged as an assumption. Indices produced here are
*proxies*, clearly labelled MODELLED/INFERRED, not regulatory AQI values.
"""
from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..core.models import TelemetryFrame
from .base import ResultType, ScientificResult
from .registry import AnalysisRegistry
from .series import SeriesBundle
from . import stats

CATEGORY = "gases"

_VOC_FIELDS = ("voc_index", "sgp41_voc", "voc")
_NOX_FIELDS = ("nox_index", "sgp41_nox", "nox")
_CO2_FIELDS = ("co2", "eco2", "co2_ppm")
# ENS160 fields and the altitude sources used to build vertical profiles.
_TVOC_FIELDS = ("ens160_tvoc", "tvoc", "tvoc_ppb")
_ECO2_FIELDS = ("ens160_eco2",) + _CO2_FIELDS
_AQI_FIELDS = ("ens160_aqi", "aqi")
_ALT_FIELDS = ("altitude", "fused_altitude", "gnss_altitude")

BOUNDARY_LAYER_TOP_M = 200.0     # "inside the boundary layer" below this
FREE_TROPOSPHERE_BASE_M = 500.0  # "free troposphere" above this


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


# ---------------------------------------------------------------------------
# ENS160 vertical profiles and gas-based boundary-layer detection.
# ---------------------------------------------------------------------------
FramesLike = Union[SeriesBundle, Sequence[TelemetryFrame]]


def _as_frames(frames: FramesLike) -> List[TelemetryFrame]:
    if isinstance(frames, SeriesBundle):
        return list(frames._frames)  # noqa: SLF001 - same package
    return list(frames or [])


def _frame_value(frame: TelemetryFrame, names: Sequence[str]) -> Optional[float]:
    """First valid, finite value among ``names`` in one frame, else None."""

    for n in names:
        m = frame.get(n)
        if m is not None and m.valid:
            v = float(m.value)
            if v == v and v not in (float("inf"), float("-inf")):
                return v
    return None


def _alt_profile(frames: Sequence[TelemetryFrame], names: Sequence[str]) -> List[Tuple[float, float]]:
    """(altitude_m, value) pairs from frames carrying both, sorted by altitude."""

    pairs = []
    for f in frames:
        z = _frame_value(f, _ALT_FIELDS)
        v = _frame_value(f, names)
        if z is not None and v is not None:
            pairs.append((z, v))
    pairs.sort(key=lambda p: p[0])
    return pairs


def analyse_ens160(frames: FramesLike) -> Dict[str, Any]:
    """Analyse ENS160 TVOC, eCO2 and AQI versus altitude.

    Returns a dict with:
      - ``tvoc_profile``: list of (alt_m, tvoc_ppb) tuples (sorted by altitude)
      - ``eco2_profile``: list of (alt_m, eco2_ppm) tuples
      - ``aqi_distribution``: Counter of integer AQI values
      - ``tvoc_boundary_layer_ppb``: median TVOC below 200 m (None if no data)
      - ``tvoc_free_troposphere_ppb``: median TVOC above 500 m (None if no data)
      - ``tvoc_reduction_factor``: free/boundary ratio if both exist, else None
    """

    fr = _as_frames(frames)
    tvoc = _alt_profile(fr, _TVOC_FIELDS)
    eco2 = _alt_profile(fr, _ECO2_FIELDS)
    aqi: Counter = Counter()
    for f in fr:
        a = _frame_value(f, _AQI_FIELDS)
        if a is not None and 1 <= round(a) <= 5:
            aqi[int(round(a))] += 1
    bl = [v for z, v in tvoc if z < BOUNDARY_LAYER_TOP_M]
    ft = [v for z, v in tvoc if z > FREE_TROPOSPHERE_BASE_M]
    bl_med = median(bl) if bl else None
    ft_med = median(ft) if ft else None
    ratio = (ft_med / bl_med) if (bl_med is not None and ft_med is not None and bl_med > 0) else None
    return {
        "tvoc_profile": tvoc,
        "eco2_profile": eco2,
        "aqi_distribution": aqi,
        "tvoc_boundary_layer_ppb": bl_med,
        "tvoc_free_troposphere_ppb": ft_med,
        "tvoc_reduction_factor": ratio,
    }


def _binned(profile: Sequence[Tuple[float, float]], bin_m: float) -> List[Tuple[float, float]]:
    """Median value per altitude bin -> [(bin_centre_m, median_value)]."""

    bins: Dict[int, List[float]] = {}
    for z, v in profile:
        bins.setdefault(int(z // bin_m), []).append(v)
    return [((k + 0.5) * bin_m, median(vs)) for k, vs in sorted(bins.items())]


def detect_boundary_layer_height(frames: FramesLike, bin_m: float = 50.0,
                                 min_bins: int = 4) -> Optional[float]:
    """Estimate the planetary boundary layer (PBL) top from the gas gradient.

    TVOC (falling back to eCO2) samples are median-binned by altitude to
    suppress sensor noise and the ascent/descent mixing of timestamps; the PBL
    top is the altitude where the concentration drops most steeply (most
    negative dC/dz between adjacent bins). Returns metres, or None if there is
    insufficient data or no decreasing gradient.
    """

    fr = _as_frames(frames)
    profile = _alt_profile(fr, _TVOC_FIELDS) or _alt_profile(fr, _ECO2_FIELDS)
    if len(profile) < min_bins or bin_m <= 0:
        return None
    binned = _binned(profile, bin_m)
    if len(binned) < min_bins:
        return None
    best_z: Optional[float] = None
    best_grad = 0.0
    for (z0, c0), (z1, c1) in zip(binned, binned[1:]):
        dz = z1 - z0
        if dz <= 0:
            continue
        grad = (c1 - c0) / dz
        if grad < best_grad:
            best_grad = grad
            best_z = 0.5 * (z0 + z1)
    return best_z


def ens160_profile(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Registry wrapper: ENS160 TVOC boundary-layer vs free-troposphere summary."""

    name = "ens160_profile"
    res = analyse_ens160(series)
    n = len(res["tvoc_profile"])
    if n < 2:
        return ScientificResult.insufficient(name, "need >=2 TVOC+altitude samples", n_samples=n, category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value={
            "tvoc_boundary_layer_ppb": res["tvoc_boundary_layer_ppb"],
            "tvoc_free_troposphere_ppb": res["tvoc_free_troposphere_ppb"],
            "tvoc_reduction_factor": res["tvoc_reduction_factor"],
            "aqi_distribution": dict(res["aqi_distribution"]),
        },
        unit="ppb",
        method=f"Median ENS160 TVOC below {BOUNDARY_LAYER_TOP_M:.0f} m vs above {FREE_TROPOSPHERE_BASE_M:.0f} m",
        result_type=ResultType.MEASURED,
        inputs={"n_tvoc": n, "n_eco2": len(res["eco2_profile"])},
        assumptions=["Altitude from barometric/GNSS fusion", "ENS160 past warm-up (status 0)"],
        uncertainty=None,
        confidence=0.6,
        limitations=["ENS160 TVOC/eCO2 are MOX-derived estimates, not reference-grade"],
        n_samples=n,
        category=CATEGORY,
    )


def gas_boundary_layer_height(series: SeriesBundle, **_: Any) -> ScientificResult:
    """Registry wrapper for :func:`detect_boundary_layer_height`."""

    name = "gas_boundary_layer_height"
    z = detect_boundary_layer_height(series)
    if z is None:
        return ScientificResult.insufficient(name, "insufficient TVOC/eCO2 profile or no decreasing gradient", category=CATEGORY)
    return ScientificResult(
        analysis_name=name,
        value=z,
        unit="m",
        method="Altitude of most negative dTVOC/dz (50 m median bins)",
        result_type=ResultType.INFERRED,
        inputs={},
        assumptions=["Pollutants well mixed below and capped at the PBL top"],
        uncertainty=25.0,
        confidence=0.4,
        limitations=["Resolution limited to the 50 m bin width", "Local plumes can bias the gradient"],
        n_samples=series.n_frames,
        category=CATEGORY,
    )


def register_all(reg: AnalysisRegistry) -> None:
    reg.register("voc_profile", CATEGORY, voc_profile, "VOC index summary", [], 2)
    reg.register("air_quality_proxy", CATEGORY, air_quality_proxy, "Air-quality proxy 0-100", [], 1)
    reg.register("plume_detection", CATEGORY, plume_detection, "VOC plume detection", [], 5)
    reg.register("ens160_profile", CATEGORY, ens160_profile, "ENS160 TVOC/eCO2/AQI vs altitude", [], 2)
    reg.register("gas_boundary_layer_height", CATEGORY, gas_boundary_layer_height,
                 "PBL top from TVOC gradient", [], 4)
