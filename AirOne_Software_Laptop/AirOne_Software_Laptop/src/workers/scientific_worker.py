"""ScientificAnalysisWorker: runs the analysis registry over a rolling window.

Consumes :class:`TelemetryFrame` objects from the scientific queue (fed by the
pipeline's stage 17 ``scientific_sink``), keeps a bounded rolling buffer, and
periodically runs every registered analysis over the current window. Results
are cached in memory (for the API) and persisted through the
:class:`AnalysisRepository`.

Design rules honoured here:
* A failing analysis never stops the others (the registry catches per-analysis
  exceptions and returns an INVALID result).
* Results are stored with their full epistemic payload.
* If persistence fails, the worker keeps running and records the error in its
  health status — it never fabricates or silently drops the science tier.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from ..core.models import TelemetryFrame
from ..scientific import build_default_registry
from ..scientific.narrative import build_narrative
from ..scientific.series import SeriesBundle
from .base_worker import BaseWorker

logger = logging.getLogger(__name__)


class ScientificAnalysisWorker(BaseWorker):
    def __init__(
        self,
        work_queue: "queue.Queue",
        analysis_repo: Any = None,
        registry: Any = None,
        window_size: int = 240,
        run_interval_s: float = 3.0,
        mission_id_provider: Optional[Callable[[], str]] = None,
        mission_state_provider: Optional[Callable[[], str]] = None,
        persist: bool = True,
    ) -> None:
        super().__init__("ScientificAnalysis")
        self.work_queue = work_queue
        self.analysis_repo = analysis_repo
        self.registry = registry or build_default_registry()
        self.window_size = window_size
        self.run_interval_s = run_interval_s
        self.mission_id_provider = mission_id_provider or (lambda: "default")
        self.mission_state_provider = mission_state_provider or (lambda: "UNKNOWN")
        self.persist = persist

        self._buffer: "deque[TelemetryFrame]" = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self._latest_results: Dict[str, Any] = {}
        self._latest_narrative: Dict[str, Any] = {}
        self._last_run = 0.0
        self.health_status.update({"analyses_run": 0, "results_stored": 0, "store_errors": 0})

    # -- public read API (thread-safe snapshots) -------------------------
    def latest_results(self) -> Dict[str, Any]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._latest_results.items()}

    def latest_narrative(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._latest_narrative)

    def run_now(self) -> Dict[str, Any]:
        """Force an analysis pass over the current buffer and return results."""

        self._run_analyses()
        return self.latest_results()

    # -- worker loop -----------------------------------------------------
    def run(self) -> None:
        while not self.should_stop():
            drained = 0
            try:
                while drained < 500:
                    frame = self.work_queue.get(timeout=0.25)
                    if isinstance(frame, TelemetryFrame):
                        with self._lock:
                            self._buffer.append(frame)
                    drained += 1
            except queue.Empty:
                pass
            self.health_status["iterations"] += 1
            now = time.time()
            if now - self._last_run >= self.run_interval_s:
                self._run_analyses()
                self._last_run = now

    def _run_analyses(self) -> None:
        with self._lock:
            frames: List[TelemetryFrame] = list(self._buffer)
        if not frames:
            return
        series = SeriesBundle(frames)
        results: Dict[str, Any] = {}
        for name in self.registry.names():
            results[name] = self.registry.run(name, series)
        self.health_status["analyses_run"] += len(results)

        mission_id = self.mission_id_provider()
        mission_state = self.mission_state_provider()
        narrative = build_narrative(results, mission_state=mission_state, mission_id=mission_id)

        with self._lock:
            self._latest_results = results
            self._latest_narrative = narrative

        if self.persist and self.analysis_repo is not None:
            # Persist only valid results to keep the table meaningful; invalid
            # ones remain visible via the in-memory snapshot / narrative.
            to_store = [r for r in results.values() if r.valid and r.value is not None]
            try:
                n = self.analysis_repo.store_many(to_store, mission_id=mission_id)
                self.health_status["results_stored"] += n
            except Exception:  # noqa: BLE001
                self.health_status["store_errors"] += 1
                logger.exception("Analysis persistence failed")
