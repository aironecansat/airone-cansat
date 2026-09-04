"""Typed repository for events."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional

from ...core.models import Event, EventSeverity
from ..database import DatabaseManager

logger = logging.getLogger(__name__)


class EventRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def store(self, event: Event) -> int:
        cur = self.db.execute_with_retry(
            """INSERT INTO events
               (mission_id, timestamp, event_type, severity, source, message,
                details, acknowledged)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.mission_id,
                event.timestamp.isoformat(),
                event.event_type,
                event.severity.value,
                event.source,
                event.message,
                json.dumps(event.details) if event.details else None,
                1 if event.acknowledged else 0,
            ),
        )
        return int(cur.lastrowid)

    def get_recent(
        self, limit: int = 100, severity_filter: Optional[str] = None
    ) -> List[Event]:
        sql = "SELECT * FROM events"
        params: list = []
        if severity_filter:
            sql += " WHERE severity = ?"
            params.append(severity_filter)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row) -> Event:
        return Event(
            timestamp=datetime.fromisoformat(row["timestamp"]),
            event_type=row["event_type"],
            severity=EventSeverity(row["severity"]),
            source=row["source"],
            message=row["message"],
            mission_id=row["mission_id"],
            details=json.loads(row["details"]) if row["details"] else None,
            acknowledged=bool(row["acknowledged"]),
        )
