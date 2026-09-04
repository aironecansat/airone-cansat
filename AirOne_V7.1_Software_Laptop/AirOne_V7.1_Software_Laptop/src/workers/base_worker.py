"""Base class for background workers.

Each worker owns its own :class:`threading.Event` stop flag. There is no global
stop event. The orchestrator coordinates ordered shutdown.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


class BaseWorker:
    def __init__(self, name: str) -> None:
        self.name = name
        self._stop_event = threading.Event()
        self._thread: threading.Thread = None  # type: ignore
        self.health_status: Dict[str, Any] = {"state": "created", "iterations": 0, "errors": 0}
        self.started_at: float = 0.0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_wrapper, name=self.name, daemon=True)
        self.started_at = time.time()
        self._thread.start()
        self.health_status["state"] = "running"
        logger.info("Worker %s started", self.name)

    def _run_wrapper(self) -> None:
        try:
            self.setup()
            self.run()
        except Exception:  # noqa: BLE001
            self.health_status["state"] = "crashed"
            self.health_status["errors"] += 1
            logger.exception("Worker %s crashed", self.name)
        finally:
            self.teardown()
            self.health_status["state"] = "stopped"

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal stop and join. Returns True if the thread ended cleanly."""

        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            clean = not self._thread.is_alive()
            if not clean:
                logger.warning("Worker %s did not stop within %.1fs", self.name, timeout)
            return clean
        return True

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    # -- overridable hooks ----------------------------------------------
    def setup(self) -> None:  # pragma: no cover - optional
        pass

    def run(self) -> None:
        raise NotImplementedError(f"Worker {self.name} must implement run()")

    def teardown(self) -> None:  # pragma: no cover - optional
        pass
