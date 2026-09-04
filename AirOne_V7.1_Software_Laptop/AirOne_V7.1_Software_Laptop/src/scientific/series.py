"""SeriesBundle: a time-aligned view over a window of telemetry frames.

The scientific worker keeps a rolling buffer of :class:`TelemetryFrame`
objects. Before running analyses it wraps that buffer in a
:class:`SeriesBundle`, which exposes per-field time series of *valid*
measurements only. Analyses ask for the fields they need and receive parallel
lists of timestamps (seconds, monotonically taken from frame time) and values.

Invalid / missing / stale measurements are excluded here so that every
analysis operates on trustworthy numbers; the count of excluded points is
available via :meth:`quality_of` for honesty reporting.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from ..core.models import Measurement, TelemetryFrame


class SeriesBundle:
    def __init__(self, frames: Sequence[TelemetryFrame]) -> None:
        self._frames: List[TelemetryFrame] = list(frames)
        # Establish a t0 so timestamps are relative seconds (float).
        self._t0 = self._frames[0].timestamp if self._frames else None

    # -- basics ----------------------------------------------------------
    @property
    def n_frames(self) -> int:
        return len(self._frames)

    def field_names(self) -> List[str]:
        names = set()
        for f in self._frames:
            names.update(f.measurements.keys())
        return sorted(names)

    def has(self, name: str) -> bool:
        return any(name in f.measurements for f in self._frames)

    # -- series extraction ----------------------------------------------
    def series(self, name: str, valid_only: bool = True) -> Tuple[List[float], List[float]]:
        """Return (t_seconds, values) for ``name`` across the window."""

        ts: List[float] = []
        vals: List[float] = []
        for f in self._frames:
            m = f.measurements.get(name)
            if m is None:
                continue
            if valid_only and not m.valid:
                continue
            t = (m.timestamp - self._t0).total_seconds() if self._t0 else 0.0
            ts.append(t)
            vals.append(float(m.value))
        return ts, vals

    def values(self, name: str, valid_only: bool = True) -> List[float]:
        return self.series(name, valid_only=valid_only)[1]

    def latest(self, name: str, valid_only: bool = True) -> Optional[Measurement]:
        for f in reversed(self._frames):
            m = f.measurements.get(name)
            if m is not None and (m.valid or not valid_only):
                return m
        return None

    def quality_of(self, name: str) -> Dict[str, int]:
        """Return counts of total / valid / invalid points for a field."""

        total = valid = 0
        for f in self._frames:
            m = f.measurements.get(name)
            if m is None:
                continue
            total += 1
            if m.valid:
                valid += 1
        return {"total": total, "valid": valid, "invalid": total - valid}

    def first_present(self, *names: str) -> Optional[str]:
        """Return the first field name that has any valid data in the window."""

        for n in names:
            if self.quality_of(n)["valid"] > 0:
                return n
        return None
