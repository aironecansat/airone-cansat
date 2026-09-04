"""Persistence worker: commits telemetry frames to SQLite."""
from __future__ import annotations

import logging
import queue
from typing import Optional

from ..core.models import TelemetryFrame
from ..storage.repositories import TelemetryRepository
from .base_worker import BaseWorker

logger = logging.getLogger(__name__)


class PersistenceWorker(BaseWorker):
    def __init__(
        self,
        frame_queue: "queue.Queue",
        telemetry_repo: TelemetryRepository,
        mission_id: str = "default",
    ) -> None:
        super().__init__("Persistence")
        self.frame_queue = frame_queue
        self.repo = telemetry_repo
        self.mission_id = mission_id

    def enqueue(self, frame: TelemetryFrame) -> None:
        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            logger.warning("Persistence queue full; dropping frame seq=%s", frame.sequence)

    def run(self) -> None:
        while not self.should_stop():
            try:
                frame = self.frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.health_status["iterations"] += 1
            try:
                raw_id = self.repo.store_raw(
                    frame.raw_bytes,
                    {
                        "source": "telemetry",
                        "crc_valid": frame.crc_valid,
                        "fec_applied": frame.fec_applied,
                        "fec_repaired": frame.fec_repaired,
                        "packet_seq": frame.sequence,
                        "mission_phase": frame.mission_phase,
                        "received_at": frame.timestamp.isoformat(),
                    },
                )
                if frame.measurements:
                    self.repo.store_measurements(
                        list(frame.measurements.values()), raw_id, self.mission_id
                    )
            except Exception:  # noqa: BLE001
                self.health_status["errors"] += 1
                logger.exception("Persistence failed for seq=%s", frame.sequence)
