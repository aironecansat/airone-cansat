"""Feature-matrix extraction for the ML tier.

Turns a window of telemetry frames into a numeric matrix suitable for anomaly
detection. Only fully-valid samples contribute a feature row; frames missing
a required field are dropped and counted, never imputed with a fabricated
value. The set of contributing feature columns is reported explicitly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

# Default feature set — physically meaningful, provenance-diverse fields.
DEFAULT_FEATURES = [
    "bme688_temperature",
    "bme688_pressure",
    "bme688_humidity",
    "gnss_altitude",
    "radiation_cpm",
    "battery_voltage",
]


@dataclass
class FeatureMatrix:
    """A dense feature matrix plus honest accounting of what was used."""

    rows: List[List[float]] = field(default_factory=list)
    feature_names: List[str] = field(default_factory=list)
    timestamps: List[str] = field(default_factory=list)
    dropped_incomplete: int = 0
    total_frames: int = 0

    @property
    def n_samples(self) -> int:
        return len(self.rows)

    @property
    def n_features(self) -> int:
        return len(self.feature_names)

    @property
    def usable(self) -> bool:
        return self.n_samples > 0 and self.n_features > 0

    def as_numpy(self):
        import numpy as np
        return np.asarray(self.rows, dtype=float)


def _valid_value(measurement: Any) -> Optional[float]:
    """Return a finite float only if the measurement is genuinely valid."""
    if measurement is None:
        return None
    valid = getattr(measurement, "valid", False)
    if not valid:
        return None
    value = getattr(measurement, "value", None)
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def extract_feature_matrix(
    frames: Sequence[Any],
    features: Optional[Sequence[str]] = None,
) -> FeatureMatrix:
    """Build a :class:`FeatureMatrix` from telemetry frames.

    A frame contributes a row only if EVERY requested feature is present and
    valid in that frame. Frames with any missing feature are dropped and
    counted (``dropped_incomplete``) — no imputation, no zero-filling.
    """
    feature_names = list(features or DEFAULT_FEATURES)
    fm = FeatureMatrix(feature_names=feature_names, total_frames=len(frames))
    for frame in frames:
        measurements = getattr(frame, "measurements", {}) or {}
        row: List[float] = []
        complete = True
        for name in feature_names:
            v = _valid_value(measurements.get(name))
            if v is None:
                complete = False
                break
            row.append(v)
        if not complete:
            fm.dropped_incomplete += 1
            continue
        fm.rows.append(row)
        ts = getattr(frame, "timestamp", None)
        fm.timestamps.append(ts.isoformat() if hasattr(ts, "isoformat") else str(ts))
    # Drop feature columns that ended up constant across all rows? No — a
    # constant column is legitimate information for a detector. Keep as-is.
    return fm
