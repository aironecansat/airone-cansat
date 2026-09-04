"""Anomaly detectors with explicit availability and honest degradation.

Detector tiers:

* :class:`RobustZScoreDetector` — pure NumPy median/MAD robust z-score.
  ALWAYS available; the guaranteed baseline.
* :class:`IsolationForestDetector`, :class:`GMMDetector` — scikit-learn.
  Available only if scikit-learn imports.
* :class:`LSTMForecastDetector` — PyTorch sequence forecaster; anomaly =
  large one-step prediction error. Available only if torch imports.
* :class:`EnsembleDetector` — averages the *available* detectors' normalised
  scores and records exactly which contributed.

Every detector exposes ``available`` and ``reason``. When unavailable, calls
return an explicit :class:`DetectorResult` with ``available=False`` — never a
fabricated score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- optional dependency probes (evaluated once) ---------------------------
try:
    import numpy as np
    _NUMPY_OK, _NUMPY_ERR = True, ""
except Exception as exc:  # pragma: no cover
    np = None  # type: ignore
    _NUMPY_OK, _NUMPY_ERR = False, str(exc)

try:
    from sklearn.ensemble import IsolationForest as _SkIForest
    from sklearn.mixture import GaussianMixture as _SkGMM
    _SKLEARN_OK, _SKLEARN_ERR = True, ""
except Exception as exc:  # pragma: no cover
    _SkIForest = _SkGMM = None  # type: ignore
    _SKLEARN_OK, _SKLEARN_ERR = False, str(exc)

try:
    import torch
    import torch.nn as nn
    _TORCH_OK, _TORCH_ERR = True, ""
except Exception as exc:  # pragma: no cover
    torch = None  # type: ignore
    nn = None  # type: ignore
    _TORCH_OK, _TORCH_ERR = False, str(exc)


@dataclass
class DetectorResult:
    """Result of scoring a matrix. Scores are higher == more anomalous."""

    detector: str
    method: str
    available: bool
    scores: List[float] = field(default_factory=list)
    is_anomaly: List[bool] = field(default_factory=list)
    threshold: Optional[float] = None
    n_samples: int = 0
    reason: str = ""
    contributors: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detector": self.detector,
            "method": self.method,
            "available": self.available,
            "scores": [None if (isinstance(s, float) and not math.isfinite(s)) else s
                       for s in self.scores],
            "is_anomaly": self.is_anomaly,
            "threshold": self.threshold,
            "n_samples": self.n_samples,
            "anomaly_count": int(sum(1 for a in self.is_anomaly if a)),
            "reason": self.reason,
            "contributors": self.contributors,
            "extra": self.extra,
        }

    @classmethod
    def unavailable(cls, detector: str, method: str, reason: str) -> "DetectorResult":
        return cls(detector=detector, method=method, available=False, reason=reason)


class BaseDetector:
    name = "base"
    method = "abstract"
    available = False
    reason = "abstract base detector"

    def fit(self, X) -> "BaseDetector":  # noqa: N803
        raise NotImplementedError

    def score(self, X) -> DetectorResult:  # noqa: N803
        raise NotImplementedError


# --------------------------------------------------------------------------
class RobustZScoreDetector(BaseDetector):
    """Median/MAD robust multivariate z-score. Pure NumPy — always available.

    Anomaly score = max over features of |x - median| / (1.4826 * MAD).
    A sample is anomalous if its score exceeds ``threshold`` (default 3.5,
    the common robust-outlier cutoff). If a feature has zero MAD it is
    excluded from that feature's contribution (division guarded), which is
    reported rather than silently producing infinities.
    """

    name = "robust_zscore"
    method = "median/MAD robust z-score (|x-med|/(1.4826*MAD)), cutoff 3.5"

    def __init__(self, threshold: float = 3.5) -> None:
        self.threshold = float(threshold)
        self.available = _NUMPY_OK
        self.reason = "" if _NUMPY_OK else f"numpy unavailable: {_NUMPY_ERR}"
        self._median = None
        self._mad = None
        self._degenerate_features: List[int] = []

    def fit(self, X):  # noqa: N803
        if not self.available:
            return self
        arr = np.asarray(X, dtype=float)
        self._median = np.median(arr, axis=0)
        abs_dev = np.abs(arr - self._median)
        self._mad = np.median(abs_dev, axis=0)
        self._degenerate_features = [int(i) for i, m in enumerate(self._mad) if m == 0]
        return self

    def score(self, X) -> DetectorResult:  # noqa: N803
        if not self.available:
            return DetectorResult.unavailable(self.name, self.method, self.reason)
        if self._median is None:
            self.fit(X)
        arr = np.asarray(X, dtype=float)
        if arr.size == 0:
            return DetectorResult(self.name, self.method, True, n_samples=0,
                                  reason="empty feature matrix")
        scale = 1.4826 * self._mad
        # Guard zero-scale features: they contribute 0 to the max (no info).
        safe_scale = np.where(scale == 0, np.inf, scale)
        z = np.abs(arr - self._median) / safe_scale
        scores = np.max(z, axis=1)
        is_anom = scores > self.threshold
        return DetectorResult(
            detector=self.name, method=self.method, available=True,
            scores=[float(s) for s in scores],
            is_anomaly=[bool(a) for a in is_anom],
            threshold=self.threshold, n_samples=int(arr.shape[0]),
            extra={"degenerate_features": self._degenerate_features},
        )


# --------------------------------------------------------------------------
class IsolationForestDetector(BaseDetector):
    """scikit-learn Isolation Forest. Available only if sklearn imports."""

    name = "isolation_forest"
    method = "sklearn IsolationForest (contamination='auto')"

    def __init__(self, n_estimators: int = 200, contamination: str = "auto",
                 random_state: int = 12345) -> None:
        self.available = _SKLEARN_OK
        self.reason = "" if _SKLEARN_OK else f"scikit-learn unavailable: {_SKLEARN_ERR}"
        self._model = None
        self._params = {"n_estimators": n_estimators,
                        "contamination": contamination,
                        "random_state": random_state}

    def fit(self, X):  # noqa: N803
        if not self.available:
            return self
        self._model = _SkIForest(**self._params)
        self._model.fit(np.asarray(X, dtype=float))
        return self

    def score(self, X) -> DetectorResult:  # noqa: N803
        if not self.available:
            return DetectorResult.unavailable(self.name, self.method, self.reason)
        if self._model is None:
            self.fit(X)
        arr = np.asarray(X, dtype=float)
        if arr.size == 0:
            return DetectorResult(self.name, self.method, True, n_samples=0,
                                  reason="empty feature matrix")
        # sklearn: higher score_samples == more normal; invert so higher==anomalous.
        raw = self._model.score_samples(arr)
        scores = [float(-s) for s in raw]
        preds = self._model.predict(arr)  # -1 anomaly, 1 normal
        is_anom = [bool(p == -1) for p in preds]
        return DetectorResult(
            detector=self.name, method=self.method, available=True,
            scores=scores, is_anomaly=is_anom, n_samples=int(arr.shape[0]),
        )


# --------------------------------------------------------------------------
class GMMDetector(BaseDetector):
    """Gaussian Mixture density model; anomaly = low log-likelihood tail."""

    name = "gmm"
    method = "sklearn GaussianMixture negative log-likelihood, 5th-pct cutoff"

    def __init__(self, n_components: int = 2, random_state: int = 12345,
                 quantile: float = 0.05) -> None:
        self.available = _SKLEARN_OK
        self.reason = "" if _SKLEARN_OK else f"scikit-learn unavailable: {_SKLEARN_ERR}"
        self._model = None
        self._threshold = None
        self._quantile = float(quantile)
        self._params = {"n_components": n_components, "random_state": random_state,
                        "covariance_type": "full"}

    def fit(self, X):  # noqa: N803
        if not self.available:
            return self
        arr = np.asarray(X, dtype=float)
        n_comp = min(self._params["n_components"], max(1, arr.shape[0]))
        params = dict(self._params, n_components=n_comp)
        self._model = _SkGMM(**params)
        self._model.fit(arr)
        ll = self._model.score_samples(arr)
        # Threshold on the low-likelihood tail of the *training* data.
        self._threshold = float(np.quantile(ll, self._quantile))
        return self

    def score(self, X) -> DetectorResult:  # noqa: N803
        if not self.available:
            return DetectorResult.unavailable(self.name, self.method, self.reason)
        if self._model is None:
            self.fit(X)
        arr = np.asarray(X, dtype=float)
        if arr.size == 0:
            return DetectorResult(self.name, self.method, True, n_samples=0,
                                  reason="empty feature matrix")
        ll = self._model.score_samples(arr)
        scores = [float(-s) for s in ll]  # higher == more anomalous
        is_anom = [bool(v < self._threshold) for v in ll]
        return DetectorResult(
            detector=self.name, method=self.method, available=True,
            scores=scores, is_anomaly=is_anom,
            threshold=float(-self._threshold) if self._threshold is not None else None,
            n_samples=int(arr.shape[0]),
        )


# --------------------------------------------------------------------------
if _TORCH_OK:
    class _LSTMNet(nn.Module):  # pragma: no cover - exercised only when torch present
        def __init__(self, n_features: int, hidden: int = 16):
            super().__init__()
            self.lstm = nn.LSTM(n_features, hidden, batch_first=True)
            self.head = nn.Linear(hidden, n_features)

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.head(out)


class LSTMForecastDetector(BaseDetector):
    """PyTorch LSTM one-step forecaster; anomaly = large prediction error.

    Available only if torch imports. Trains a small LSTM to predict the next
    feature vector from the previous ``seq_len`` steps; the per-sample anomaly
    score is the normalised forecast error.
    """

    name = "lstm_forecast"
    method = "PyTorch LSTM 1-step forecast error (z-scored residual, cutoff 3.0)"

    def __init__(self, seq_len: int = 8, hidden: int = 16, epochs: int = 40,
                 lr: float = 0.01, threshold: float = 3.0, seed: int = 12345) -> None:
        self.available = _TORCH_OK
        self.reason = "" if _TORCH_OK else f"PyTorch unavailable: {_TORCH_ERR}"
        self.seq_len = seq_len
        self.hidden = hidden
        self.epochs = epochs
        self.lr = lr
        self.threshold = threshold
        self.seed = seed
        self._net = None
        self._mu = None
        self._sigma = None
        self._err_mean = None
        self._err_std = None

    def _standardise(self, arr):
        return (arr - self._mu) / np.where(self._sigma == 0, 1.0, self._sigma)

    def fit(self, X):  # noqa: N803  # pragma: no cover - needs torch
        if not self.available:
            return self
        torch.manual_seed(self.seed)
        arr = np.asarray(X, dtype=float)
        if arr.shape[0] <= self.seq_len + 1:
            self.reason = "insufficient samples for LSTM sequence window"
            self._net = None
            return self
        self._mu = arr.mean(axis=0)
        self._sigma = arr.std(axis=0)
        std = self._standardise(arr)
        xs, ys = [], []
        for i in range(len(std) - self.seq_len):
            xs.append(std[i:i + self.seq_len])
            ys.append(std[i + self.seq_len])
        xt = torch.tensor(np.asarray(xs), dtype=torch.float32)
        yt = torch.tensor(np.asarray(ys), dtype=torch.float32)
        self._net = _LSTMNet(arr.shape[1], self.hidden)
        opt = torch.optim.Adam(self._net.parameters(), lr=self.lr)
        loss_fn = nn.MSELoss()
        self._net.train()
        for _ in range(self.epochs):
            opt.zero_grad()
            pred = self._net(xt)[:, -1, :]
            loss = loss_fn(pred, yt)
            loss.backward()
            opt.step()
        self._net.eval()
        with torch.no_grad():
            pred = self._net(xt)[:, -1, :].numpy()
        errs = np.linalg.norm(pred - np.asarray(ys), axis=1)
        self._err_mean = float(errs.mean())
        self._err_std = float(errs.std() or 1.0)
        return self

    def score(self, X) -> DetectorResult:  # noqa: N803  # pragma: no cover - needs torch
        if not self.available:
            return DetectorResult.unavailable(self.name, self.method, self.reason)
        if self._net is None:
            return DetectorResult(self.name, self.method, True, n_samples=0,
                                  reason=self.reason or "model not fitted")
        arr = np.asarray(X, dtype=float)
        std = self._standardise(arr)
        scores = [0.0] * len(arr)
        is_anom = [False] * len(arr)
        with torch.no_grad():
            for i in range(self.seq_len, len(std)):
                window = torch.tensor(std[i - self.seq_len:i][None, :, :],
                                      dtype=torch.float32)
                pred = self._net(window)[0, -1, :].numpy()
                err = float(np.linalg.norm(pred - std[i]))
                z = (err - self._err_mean) / self._err_std
                scores[i] = z
                is_anom[i] = bool(z > self.threshold)
        return DetectorResult(
            detector=self.name, method=self.method, available=True,
            scores=scores, is_anomaly=is_anom, threshold=self.threshold,
            n_samples=int(arr.shape[0]),
            extra={"note": "first seq_len samples have no forecast (score 0)"},
        )


# --------------------------------------------------------------------------
class EnsembleDetector(BaseDetector):
    """Average of the *available* detectors' rank-normalised anomaly scores.

    Records exactly which detectors contributed. A sample is flagged if at
    least ``vote_fraction`` of contributing detectors flag it. If no detector
    is available (should never happen — robust z-score is pure NumPy), the
    result is explicitly unavailable rather than fabricated.
    """

    name = "ensemble"
    method = "mean of rank-normalised scores across available detectors"

    def __init__(self, detectors: Optional[List[BaseDetector]] = None,
                 vote_fraction: float = 0.5) -> None:
        self._detectors = detectors or default_detectors()
        self.vote_fraction = float(vote_fraction)
        self.available = _NUMPY_OK and any(d.available for d in self._detectors)
        self.reason = "" if self.available else "no detectors available"

    def fit(self, X):  # noqa: N803
        for d in self._detectors:
            if d.available:
                try:
                    d.fit(X)
                except Exception as exc:  # noqa: BLE001
                    d.reason = f"fit failed: {exc}"
        return self

    def score(self, X) -> DetectorResult:  # noqa: N803
        if not self.available:
            return DetectorResult.unavailable(self.name, self.method, self.reason)
        arr = np.asarray(X, dtype=float)
        n = int(arr.shape[0]) if arr.ndim == 2 else 0
        if n == 0:
            return DetectorResult(self.name, self.method, True, n_samples=0,
                                  reason="empty feature matrix")
        contributors: List[str] = []
        norm_scores = []
        flags = []
        for d in self._detectors:
            res = d.score(arr)
            if not res.available or res.n_samples != n or not res.scores:
                continue
            contributors.append(d.name)
            s = np.asarray(res.scores, dtype=float)
            # Rank-normalise to [0,1] so heterogeneous scales combine fairly.
            order = s.argsort().argsort()
            norm = order / max(1, (n - 1))
            norm_scores.append(norm)
            flags.append(np.asarray(res.is_anomaly, dtype=float))
        if not contributors:
            return DetectorResult(self.name, self.method, True, n_samples=n,
                                  reason="no contributing detector produced scores")
        mean_scores = np.mean(np.vstack(norm_scores), axis=0)
        vote = np.mean(np.vstack(flags), axis=0)
        is_anom = [bool(v >= self.vote_fraction) for v in vote]
        return DetectorResult(
            detector=self.name, method=self.method, available=True,
            scores=[float(s) for s in mean_scores], is_anomaly=is_anom,
            threshold=self.vote_fraction, n_samples=n, contributors=contributors,
            extra={"vote_fraction": self.vote_fraction},
        )


# --------------------------------------------------------------------------
def default_detectors() -> List[BaseDetector]:
    """Instantiate one of each detector (unavailable ones self-report)."""
    return [
        RobustZScoreDetector(),
        IsolationForestDetector(),
        GMMDetector(),
        LSTMForecastDetector(),
    ]


def live_detectors() -> List[BaseDetector]:
    """Detectors suitable for continuous live scoring.

    Excludes the LSTM forecaster: training a neural net on every rolling-window
    pass (every few seconds) is disproportionately expensive and would make the
    worker unresponsive to shutdown. The LSTM remains available for explicit,
    on-demand batch training via ``POST /api/v1/ml/train``. This is a
    performance decision, stated openly — not a silent capability reduction.
    """
    return [
        RobustZScoreDetector(),
        IsolationForestDetector(),
        GMMDetector(),
    ]


def available_detectors() -> Dict[str, Dict[str, Any]]:
    """Report availability + reason for every detector class."""
    out: Dict[str, Dict[str, Any]] = {}
    for d in default_detectors():
        out[d.name] = {"available": d.available, "method": d.method,
                       "reason": d.reason}
    return out
