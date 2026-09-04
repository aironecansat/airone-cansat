"""Typed repository for persisted scientific analysis results.

Stores :class:`ScientificResult` objects in the ``analysis_results`` table.
The full epistemic payload (method, inputs, assumptions, uncertainty,
confidence, limitations) is preserved so a result can always be audited and
reproduced — never just a bare number.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..database import DatabaseManager

logger = logging.getLogger(__name__)


class AnalysisRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def store(self, result: Any, mission_id: str = "default") -> int:
        """Persist a ScientificResult (duck-typed via ``to_dict``)."""

        d = result.to_dict()
        cur = self.db.execute_with_retry(
            """INSERT INTO analysis_results
               (mission_id, analysis_name, timestamp, result_data, method,
                inputs, assumptions, uncertainty, confidence, limitations)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mission_id,
                d.get("analysis_name"),
                d.get("timestamp"),
                json.dumps({
                    "value": d.get("value"),
                    "unit": d.get("unit"),
                    "result_type": d.get("result_type"),
                    "valid": d.get("valid"),
                    "n_samples": d.get("n_samples"),
                    "category": d.get("category"),
                    "extra": d.get("extra"),
                }),
                d.get("method"),
                json.dumps(d.get("inputs")),
                json.dumps(d.get("assumptions")),
                None if d.get("uncertainty") is None else json.dumps(d.get("uncertainty")),
                d.get("confidence"),
                json.dumps(d.get("limitations")),
            ),
        )
        return int(cur.lastrowid)

    def store_many(self, results: List[Any], mission_id: str = "default") -> int:
        n = 0
        for r in results:
            try:
                self.store(r, mission_id=mission_id)
                n += 1
            except Exception:  # noqa: BLE001
                logger.exception("Failed to store analysis result %s", getattr(r, "analysis_name", "?"))
        return n

    def _row_to_dict(self, row) -> Dict[str, Any]:
        def _loads(s):
            if s is None:
                return None
            try:
                return json.loads(s)
            except (TypeError, json.JSONDecodeError):
                return s

        return {
            "id": row["id"],
            "mission_id": row["mission_id"],
            "analysis_name": row["analysis_name"],
            "timestamp": row["timestamp"],
            "result": _loads(row["result_data"]),
            "method": row["method"],
            "inputs": _loads(row["inputs"]),
            "assumptions": _loads(row["assumptions"]),
            "uncertainty": _loads(row["uncertainty"]),
            "confidence": row["confidence"],
            "limitations": _loads(row["limitations"]),
        }

    def get_by_name(self, analysis_name: str, mission_id: str = "default", limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.db.query(
            """SELECT * FROM analysis_results
               WHERE analysis_name = ? AND mission_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (analysis_name, mission_id, limit),
        )
        return [self._row_to_dict(r) for r in rows]

    def latest_per_analysis(self, mission_id: str = "default") -> List[Dict[str, Any]]:
        rows = self.db.query(
            """SELECT a.* FROM analysis_results a
               JOIN (SELECT analysis_name, MAX(timestamp) AS mts
                     FROM analysis_results WHERE mission_id = ?
                     GROUP BY analysis_name) x
               ON a.analysis_name = x.analysis_name AND a.timestamp = x.mts
               WHERE a.mission_id = ?""",
            (mission_id, mission_id),
        )
        return [self._row_to_dict(r) for r in rows]

    def get_all(self, mission_id: str = "default", limit: int = 500) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM analysis_results WHERE mission_id = ? ORDER BY timestamp DESC LIMIT ?",
            (mission_id, limit),
        )
        return [self._row_to_dict(r) for r in rows]
