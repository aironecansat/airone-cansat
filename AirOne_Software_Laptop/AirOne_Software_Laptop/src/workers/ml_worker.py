"""ML analysis worker (advisory tier).

Consumes telemetry frames into a rolling buffer and periodically runs the
active anomaly detector over the window, exposing the latest scores and a
drift assessment for operators and the API.

HARD RULE: this worker holds NO reference to the mission state machine and
cannot change mission state. It is Tier-3 (ML) in the capability hierarchy;
its failures never corrupt Tier-1 (raw telemetry) or Tier-2 (science). If no
model has been trained, it says so explicitly and produces no scores rather
than fabricating anomalies.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from .base_worker import BaseWorker
from ..ml.detectors import EnsembleDetector, live_detectors
from ..ml.drift import DriftMonitor
from ..ml.features import DEFAULT_FEATURES, extract_feature_matrix


class MLAnalysisWorker(BaseWorker):
    def __init__(
        self,
        work_queue: "queue.Queue",
        window_size: int = 240,
        run_interval_s: float = 5.0,
        features: Optional[List[str]] = None,
        min_samples: int = 20,
        auto_fit: bool = True,
    ) -> None:
        super().__init__("MLAnalysis")
        self.work_queue = work_queue
        self.window_size = window_size
        self.run_interval_s = run_interval_s
        self.features = list(features or DEFAULT_FEATURES)
        self.min_samples = min_samples
        self.auto_fit = auto_fit

        self._buffer: "deque[Any]" = deque(maxlen=window_size)
        self._lock = threading.Lock()
        # Live scoring uses the lightweight detector set (no per-pass LSTM
        # training); the LSTM remains available for explicit batch training.
        self._detector = EnsembleDetector(detectors=live_detectors())
        self._drift = DriftMonitor()
        self._latest: Dict[str, Any] = {
            "state": "IDLE",
            "reason": "no analysis run yet",
        }
        self._last_run = 0.0
        self.health_status.update({"analyses_run": 0, "last_anomaly_count": 0})

    # -- read API ----------------------------------------------------------
    def latest_summary(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._latest)

    def detector_availability(self) -> Dict[str, Any]:
        from ..ml.detectors import available_detectors
        return available_detectors()

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        while not self.should_stop():
            drained = 0
            try:
                while drained < 500:
                    frame = self.work_queue.get(timeout=0.2)
                    with self._lock:
                        self._buffer.append(frame)
                    drained += 1
            except queue.Empty:
                pass

            now = time.time()
            if now - self._last_run >= self.run_interval_s:
                self._last_run = now
                self._run_once()
            self.health_status["iterations"] += 1

    def _run_once(self) -> None:
        with self._lock:
            frames = list(self._buffer)
        fm = extract_feature_matrix(frames, self.features)
        if fm.n_samples < self.min_samples:
            summary = {
                "state": "INSUFFICIENT_DATA",
                "reason": (f"{fm.n_samples} valid samples < min {self.min_samples}; "
                           f"{fm.dropped_incomplete} incomplete frames dropped"),
                "n_samples": fm.n_samples,
                "features": fm.feature_names,
            }
            with self._lock:
                self._latest = summary
            return
        if self.auto_fit:
            self._detector.fit(fm.rows)
            if not self._drift.has_reference():
                self._drift.set_reference(fm.rows, fm.feature_names)
        result = self._detector.score(fm.rows)
        drift = [d.to_dict() for d in self._drift.evaluate(fm.rows, fm.feature_names)] \
            if self._drift.has_reference() else []
        if not result.available:
            summary = {"state": "UNAVAILABLE", "reason": result.reason,
                       "detectors": self.detector_availability()}
        else:
            anomaly_count = int(sum(1 for a in result.is_anomaly if a))
            self.health_status["last_anomaly_count"] = anomaly_count
            summary = {
                "state": "AVAILABLE",
                "advisory": True,
                "note": "ML is advisory only and cannot change mission state.",
                "result": result.to_dict(),
                "drift": drift,
                "detectors": self.detector_availability(),
            }
        self.health_status["analyses_run"] += 1
        with self._lock:
            self._latest = summary
