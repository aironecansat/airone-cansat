"""Typed repository for mission state transitions."""
from __future__ import annotations

import json
import logging
from typing import Optional

from ...core.models import MissionState, MissionTransition
from ..database import DatabaseManager

logger = logging.getLogger(__name__)


class MissionRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def store_transition(
        self, transition: MissionTransition, mission_id: str = "default"
    ) -> int:
        supporting = json.dumps(
            [m.to_dict() for m in transition.supporting_measurements]
        )
        cur = self.db.execute_with_retry(
            """INSERT INTO mission_transitions
               (mission_id, timestamp, prev_state, new_state, reason, confidence,
                supporting_data)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mission_id,
                transition.timestamp.isoformat(),
                transition.prev_state.value,
                transition.new_state.value,
                transition.reason,
                transition.confidence,
                supporting,
            ),
        )
        return int(cur.lastrowid)

    def get_current_state(self, mission_id: str = "default") -> MissionState:
        row = self.db.query_one(
            """SELECT new_state FROM mission_transitions
               WHERE mission_id = ? ORDER BY id DESC LIMIT 1""",
            (mission_id,),
        )
        if row is None:
            return MissionState.BOOT
        return MissionState(row["new_state"])
