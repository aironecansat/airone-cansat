"""Typed repository for raw telemetry and processed measurements."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...core.models import DataSource, Measurement, QualityState
from ..database import DatabaseManager

logger = logging.getLogger(__name__)


class TelemetryRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def store_raw(self, raw_bytes: bytes, metadata: Dict[str, Any]) -> int:
        cur = self.db.execute_with_retry(
            """INSERT INTO raw_telemetry
               (received_at, source, receiver_id, rssi, snr, decode_status,
                crc_valid, fec_applied, fec_repaired, packet_seq, mission_phase,
                firmware_version, raw_bytes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                metadata.get("received_at", datetime.utcnow().isoformat()),
                metadata.get("source", "unknown"),
                metadata.get("receiver_id"),
                metadata.get("rssi"),
                metadata.get("snr"),
                metadata.get("decode_status", "ok"),
                1 if metadata.get("crc_valid", True) else 0,
                1 if metadata.get("fec_applied", False) else 0,
                1 if metadata.get("fec_repaired", False) else 0,
                metadata.get("packet_seq"),
                metadata.get("mission_phase"),
                metadata.get("firmware_version"),
                raw_bytes,
            ),
        )
        return int(cur.lastrowid)

    def store_measurements(
        self, measurements: List[Measurement], raw_id: Optional[int] = None,
        mission_id: str = "default",
    ) -> int:
        rows = [
            (
                raw_id,
                m.sensor_id,
                m.field_name or m.sensor_id,
                None if m.value != m.value else float(m.value),  # NaN -> NULL
                m.unit,
                m.timestamp.isoformat(),
                m.quality.value,
                1 if m.valid else 0,
                None if m.uncertainty in (float("inf"), float("-inf")) else float(m.uncertainty),
                m.calibration_version,
                m.source.value,
                m.processing_stage,
                mission_id,
            )
            for m in measurements
        ]
        self.db.executemany(
            """INSERT INTO measurements
               (raw_telemetry_id, sensor_id, field_name, value, unit, timestamp,
                quality, valid, uncertainty, calibration_version, source,
                processing_stage, mission_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return len(rows)

    def get_history(
        self,
        sensor_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        quality_filter: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Measurement]:
        sql = "SELECT * FROM measurements WHERE sensor_id = ?"
        params: List[Any] = [sensor_id]
        if start:
            sql += " AND timestamp >= ?"
            params.append(start)
        if end:
            sql += " AND timestamp <= ?"
            params.append(end)
        if quality_filter:
            sql += " AND quality = ?"
            params.append(quality_filter)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        return [self._row_to_measurement(r) for r in rows]

    def latest_per_sensor(self, mission_id: str = "default") -> List[Measurement]:
        rows = self.db.query(
            """SELECT m.* FROM measurements m
               JOIN (SELECT sensor_id, MAX(timestamp) AS mts
                     FROM measurements WHERE mission_id = ? GROUP BY sensor_id) x
               ON m.sensor_id = x.sensor_id AND m.timestamp = x.mts
               WHERE m.mission_id = ?""",
            (mission_id, mission_id),
        )
        return [self._row_to_measurement(r) for r in rows]

    @staticmethod
    def _row_to_measurement(row) -> Measurement:
        value = row["value"]
        return Measurement(
            value=float("nan") if value is None else float(value),
            unit=row["unit"] or "",
            timestamp=datetime.fromisoformat(row["timestamp"]),
            sensor_id=row["sensor_id"],
            quality=QualityState(row["quality"]),
            valid=bool(row["valid"]),
            uncertainty=float(row["uncertainty"]) if row["uncertainty"] is not None else 0.0,
            calibration_version=row["calibration_version"] or "none",
            source=DataSource(row["source"]),
            processing_stage=int(row["processing_stage"] or 0),
            field_name=row["field_name"] or "",
        )
