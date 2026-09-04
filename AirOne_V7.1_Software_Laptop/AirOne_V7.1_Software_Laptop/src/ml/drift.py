"""Feature-distribution drift monitoring.

Compares a live feature window against a reference distribution captured at
training time using the Population Stability Index (PSI). PSI is a standard,
interpretable drift metric:

    PSI < 0.1   : STABLE      (no significant shift)
    0.1–0.25    : MODERATE    (investigate)
    > 0.25      : SIGNIFICANT (distribution has shifted materially)

Drift is a *diagnostic* only — it never triggers a mission action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

try:
    import numpy as np
    _NUMPY_OK = True
except Exception:  # pragma: no cover
    np = None  # type: ignore
    _NUMPY_OK = False


def population_stability_index(
    expected: Sequence[float], actual: Sequence[float], bins: int = 10,
) -> Optional[float]:
    """Return the PSI between an expected and an actual sample, or None.

    None is returned (never a fake 0) when there is insufficient data or the
    numeric backend is unavailable.
    """
    if not _NUMPY_OK:
        return None
    exp = np.asarray(expected, dtype=float)
    act = np.asarray(actual, dtype=float)
    exp = exp[np.isfinite(exp)]
    act = act[np.isfinite(act)]
    if exp.size < bins or act.size < 1:
        return None
    # Bin edges from the expected distribution's quantiles.
    quantiles = np.linspace(0, 1, bins + 1)
    edges = np.unique(np.quantile(exp, quantiles))
    if edges.size < 2:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    exp_counts, _ = np.histogram(exp, bins=edges)
    act_counts, _ = np.histogram(act, bins=edges)
    eps = 1e-6
    exp_frac = exp_counts / max(1, exp.size) + eps
    act_frac = act_counts / max(1, act.size) + eps
    psi = np.sum((act_frac - exp_frac) * np.log(act_frac / exp_frac))
    return float(psi)


def classify_psi(psi: Optional[float]) -> str:
    if psi is None:
        return "UNAVAILABLE"
    if psi < 0.1:
        return "STABLE"
    if psi < 0.25:
        return "MODERATE"
    return "SIGNIFICANT"


@dataclass
class DriftResult:
    feature: str
    psi: Optional[float]
    status: str
    n_reference: int
    n_actual: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature": self.feature, "psi": self.psi, "status": self.status,
            "n_reference": self.n_reference, "n_actual": self.n_actual,
        }


@dataclass
class DriftMonitor:
    """Holds a reference distribution per feature and scores live windows."""

    feature_names: List[str] = field(default_factory=list)
    _reference: Dict[str, List[float]] = field(default_factory=dict)

    def set_reference(self, matrix: Sequence[Sequence[float]],
                      feature_names: Sequence[str]) -> None:
        self.feature_names = list(feature_names)
        self._reference = {name: [] for name in self.feature_names}
        for row in matrix:
            for name, value in zip(self.feature_names, row):
                self._reference[name].append(float(value))

    def has_reference(self) -> bool:
        return bool(self._reference) and all(
            len(v) > 0 for v in self._reference.values()
        )

    def evaluate(self, matrix: Sequence[Sequence[float]],
                 feature_names: Sequence[str]) -> List[DriftResult]:
        results: List[DriftResult] = []
        columns: Dict[str, List[float]] = {name: [] for name in feature_names}
        for row in matrix:
            for name, value in zip(feature_names, row):
                columns[name].append(float(value))
        for name in feature_names:
            ref = self._reference.get(name)
            if not ref:
                results.append(DriftResult(name, None, "UNAVAILABLE", 0,
                                           len(columns[name])))
                continue
            psi = population_stability_index(ref, columns[name])
            results.append(DriftResult(name, psi, classify_psi(psi),
                                       len(ref), len(columns[name])))
        return results
