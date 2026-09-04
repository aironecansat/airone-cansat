"""Training pipeline for the ML tier.

Trains an anomaly detector over a window of telemetry frames, persists the
fitted artefact with an integrity checksum, records full provenance
(training-data hash, feature list, metrics) in the model registry, and
returns a structured summary. Training never fabricates results: if there is
insufficient data or the requested backend is unavailable, it returns an
explicit failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .detectors import (
    BaseDetector, EnsembleDetector, GMMDetector, IsolationForestDetector,
    LSTMForecastDetector, RobustZScoreDetector,
)
from .drift import DriftMonitor
from .features import DEFAULT_FEATURES, extract_feature_matrix
from .provenance import hash_array
from .registry import ModelStore, SafeLoadError

logger = logging.getLogger(__name__)

DETECTOR_TYPES = {
    "robust_zscore": RobustZScoreDetector,
    "isolation_forest": IsolationForestDetector,
    "gmm": GMMDetector,
    "lstm_forecast": LSTMForecastDetector,
    "ensemble": EnsembleDetector,
}

MIN_TRAINING_SAMPLES = 20


@dataclass
class TrainingOutcome:
    ok: bool
    model_type: str
    reason: str = ""
    n_samples: int = 0
    features: List[str] = field(default_factory=list)
    training_data_hash: str = ""
    file_path: str = ""
    sha256_hash: str = ""
    version: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__


def make_detector(model_type: str, **params: Any) -> Optional[BaseDetector]:
    cls = DETECTOR_TYPES.get(model_type)
    if cls is None:
        return None
    try:
        return cls(**params) if params else cls()
    except TypeError:
        return cls()


def train_detector(
    frames: Sequence[Any],
    model_type: str = "ensemble",
    features: Optional[Sequence[str]] = None,
    store: Optional[ModelStore] = None,
    drift_monitor: Optional[DriftMonitor] = None,
    params: Optional[Dict[str, Any]] = None,
) -> TrainingOutcome:
    """Train and (if a store is given) persist an anomaly detector."""
    features = list(features or DEFAULT_FEATURES)
    detector = make_detector(model_type, **(params or {}))
    if detector is None:
        return TrainingOutcome(False, model_type,
                               reason=f"unknown model_type '{model_type}'")
    if not detector.available:
        return TrainingOutcome(False, model_type,
                               reason=f"detector unavailable: {detector.reason}")

    fm = extract_feature_matrix(frames, features)
    if fm.n_samples < MIN_TRAINING_SAMPLES:
        return TrainingOutcome(
            False, model_type, n_samples=fm.n_samples, features=fm.feature_names,
            reason=(f"insufficient training samples: {fm.n_samples} valid "
                    f"(need >= {MIN_TRAINING_SAMPLES}); "
                    f"{fm.dropped_incomplete} frames dropped as incomplete"),
        )

    X = fm.rows
    detector.fit(X)
    result = detector.score(X)
    if not result.available:
        return TrainingOutcome(False, model_type, n_samples=fm.n_samples,
                               features=fm.feature_names,
                               reason=f"scoring failed: {result.reason}")

    tdh = hash_array(X, fm.feature_names)
    anomaly_rate = (sum(1 for a in result.is_anomaly if a) / max(1, len(result.is_anomaly)))
    metrics = {
        "train_samples": fm.n_samples,
        "train_anomaly_rate": round(anomaly_rate, 4),
        "dropped_incomplete_frames": fm.dropped_incomplete,
        "contributors": result.contributors,
    }
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    file_path, sha = "", ""
    if store is not None and store.available:
        try:
            file_path, sha = store.save(detector, model_type, version)
        except SafeLoadError as exc:
            logger.warning("Model persistence skipped: %s", exc)

    if drift_monitor is not None:
        drift_monitor.set_reference(X, fm.feature_names)

    return TrainingOutcome(
        ok=True, model_type=model_type, n_samples=fm.n_samples,
        features=fm.feature_names, training_data_hash=tdh, file_path=file_path,
        sha256_hash=sha, version=version, metrics=metrics,
    )
