"""AirOne V7.1 machine-learning tier (advisory only).

Hard architectural rule enforced throughout this package: **the ML tier is
advisory**. It never imports, references, or mutates the mission state
machine. Its outputs are anomaly scores and diagnostics for human operators;
they can never command a mission-state transition.

Every model degrades explicitly: a robust median/MAD detector implemented in
pure NumPy is ALWAYS available, while scikit-learn (Isolation Forest, GMM)
and PyTorch (LSTM forecaster) detectors are used only when those libraries
import successfully. Unavailable detectors report the reason; they are never
silently skipped or faked.
"""
from __future__ import annotations

from .detectors import (
    DetectorResult,
    EnsembleDetector,
    GMMDetector,
    IsolationForestDetector,
    LSTMForecastDetector,
    RobustZScoreDetector,
    available_detectors,
    live_detectors,
)
from .drift import DriftMonitor, DriftResult, population_stability_index
from .features import FeatureMatrix, extract_feature_matrix
from .provenance import hash_array, sha256_of_bytes, sha256_of_file
from .registry import ModelStore, SafeLoadError

__all__ = [
    "DetectorResult", "RobustZScoreDetector", "IsolationForestDetector",
    "GMMDetector", "LSTMForecastDetector", "EnsembleDetector",
    "available_detectors", "live_detectors", "FeatureMatrix", "extract_feature_matrix",
    "DriftMonitor", "DriftResult", "population_stability_index",
    "ModelStore", "SafeLoadError", "hash_array", "sha256_of_bytes",
    "sha256_of_file",
]
