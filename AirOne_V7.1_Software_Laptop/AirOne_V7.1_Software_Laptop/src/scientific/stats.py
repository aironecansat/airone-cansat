"""Small, dependency-light statistical helpers used across analyses.

These use numpy where available. Every helper returns explicit ``None`` (never
a fabricated value) when the input is too small or degenerate. Uncertainty is
reported wherever a standard formula exists.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

try:  # numpy is a hard requirement listed in requirements, but degrade safely.
    import numpy as np
    _NUMPY = True
except Exception:  # pragma: no cover
    np = None  # type: ignore
    _NUMPY = False


def clean(values: Sequence[float]) -> List[float]:
    """Return only the finite values from ``values``."""

    return [float(v) for v in values if v is not None and math.isfinite(float(v))]


def mean_std(values: Sequence[float]) -> Optional[Tuple[float, float]]:
    """Return (mean, sample standard deviation) or ``None`` if <2 points."""

    v = clean(values)
    if len(v) < 2:
        return None
    if _NUMPY:
        arr = np.asarray(v, dtype=float)
        return float(arr.mean()), float(arr.std(ddof=1))
    m = sum(v) / len(v)
    var = sum((x - m) ** 2 for x in v) / (len(v) - 1)
    return m, math.sqrt(var)


def linear_regression(
    x: Sequence[float], y: Sequence[float]
) -> Optional[dict]:
    """Ordinary least squares fit ``y = slope*x + intercept``.

    Returns a dict with slope, intercept, r (Pearson), r_squared,
    slope_stderr, and n. Returns ``None`` for fewer than 3 valid pairs or a
    degenerate (zero-variance) x.
    """

    pairs = [
        (float(a), float(b))
        for a, b in zip(x, y)
        if a is not None and b is not None
        and math.isfinite(float(a)) and math.isfinite(float(b))
    ]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(pairs)
    if _NUMPY:
        ax = np.asarray(xs)
        ay = np.asarray(ys)
        if float(ax.std()) == 0.0:
            return None
        slope, intercept = np.polyfit(ax, ay, 1)
        yhat = slope * ax + intercept
        ss_res = float(np.sum((ay - yhat) ** 2))
        ss_tot = float(np.sum((ay - ay.mean()) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        # Standard error of the slope.
        dof = n - 2
        if dof > 0 and ss_tot > 0:
            s_err = math.sqrt(ss_res / dof) / math.sqrt(float(np.sum((ax - ax.mean()) ** 2)))
        else:
            s_err = float("nan")
        r = float(np.corrcoef(ax, ay)[0, 1]) if ax.std() > 0 and ay.std() > 0 else 0.0
        return {
            "slope": float(slope),
            "intercept": float(intercept),
            "r": r,
            "r_squared": float(r_squared),
            "slope_stderr": float(s_err),
            "n": n,
        }
    # Pure-python fallback.
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((xi - mx) ** 2 for xi in xs)
    if sxx == 0.0:
        return None
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(xs, ys))
    syy = sum((yi - my) ** 2 for yi in ys)
    slope = sxy / sxx
    intercept = my - slope * mx
    r = sxy / math.sqrt(sxx * syy) if syy > 0 else 0.0
    r_squared = r * r
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(xs, ys))
    dof = n - 2
    s_err = math.sqrt(ss_res / dof) / math.sqrt(sxx) if dof > 0 else float("nan")
    return {
        "slope": slope,
        "intercept": intercept,
        "r": r,
        "r_squared": r_squared,
        "slope_stderr": s_err,
        "n": n,
    }


def pearson_r(x: Sequence[float], y: Sequence[float]) -> Optional[Tuple[float, int]]:
    """Return (pearson_r, n) or ``None`` for <3 valid pairs / zero variance."""

    fit = linear_regression(x, y)
    if fit is None:
        return None
    return fit["r"], fit["n"]
