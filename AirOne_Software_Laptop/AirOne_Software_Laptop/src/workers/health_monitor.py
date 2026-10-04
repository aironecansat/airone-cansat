"""Health monitor worker: periodically snapshots system health to SQLite."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ..storage.database import DatabaseManager
from .base_worker import BaseWorker

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore

    _PSUTIL = True
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore
    _PSUTIL = False


class HealthMonitorWorker(BaseWorker):
    def __init__(
        self,
        db: DatabaseManager,
        interval_s: float = 10.0,
        queue_depth_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        super().__init__("HealthMonitor")
        self.db = db
        self.interval_s = interval_s
        self.queue_depth_provider = queue_depth_provider

    def snapshot(self) -> Dict[str, float]:
        return {
            "cpu_percent": psutil.cpu_percent() if _PSUTIL else 0.0,
            "ram_percent": psutil.virtual_memory().percent if _PSUTIL else 0.0,
            "disk_percent": psutil.disk_usage("/").percent if _PSUTIL else 0.0,
            "queue_depth": self.queue_depth_provider() if self.queue_depth_provider else 0,
            "active_threads": threading.active_count(),
        }

    def run(self) -> None:
        while not self.should_stop():
            self.health_status["iterations"] += 1
            try:
                s = self.snapshot()
                self.db.execute_with_retry(
                    """INSERT INTO system_health
                       (timestamp, cpu_percent, ram_percent, disk_percent,
                        telemetry_rate, packet_loss_rate, queue_depth,
                        api_latency_ms, active_threads)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        s["cpu_percent"], s["ram_percent"], s["disk_percent"],
                        0.0, 0.0, int(s["queue_depth"]), 0.0, int(s["active_threads"]),
                    ),
                )
            except Exception:  # noqa: BLE001
                self.health_status["errors"] += 1
                logger.exception("Health snapshot failed")
            self._stop_event.wait(self.interval_s)
