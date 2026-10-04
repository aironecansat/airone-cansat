"""Typed repository for the ML model registry."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..database import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass
class ModelMetadata:
    name: str
    version: str
    model_type: str
    file_path: Optional[str] = None
    sha256_hash: Optional[str] = None
    training_data_hash: Optional[str] = None
    features: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    is_active: bool = False
    id: Optional[int] = None


class MLModelRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def register(self, model: ModelMetadata) -> int:
        cur = self.db.execute_with_retry(
            """INSERT INTO ml_models
               (name, version, model_type, file_path, sha256_hash,
                training_data_hash, features, parameters, metrics, created_at,
                created_by, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                model.name, model.version, model.model_type, model.file_path,
                model.sha256_hash, model.training_data_hash,
                json.dumps(model.features), json.dumps(model.parameters),
                json.dumps(model.metrics),
                model.created_at or datetime.now(timezone.utc).isoformat(),
                model.created_by, 1 if model.is_active else 0,
            ),
        )
        return int(cur.lastrowid)

    def get_active(self, model_type: str) -> Optional[ModelMetadata]:
        row = self.db.query_one(
            "SELECT * FROM ml_models WHERE model_type = ? AND is_active = 1 "
            "ORDER BY id DESC LIMIT 1",
            (model_type,),
        )
        return self._row_to_meta(row) if row else None

    def get_by_id(self, model_id: int) -> Optional[ModelMetadata]:
        row = self.db.query_one("SELECT * FROM ml_models WHERE id = ?", (model_id,))
        return self._row_to_meta(row) if row else None

    def list_all(self) -> List[ModelMetadata]:
        rows = self.db.query("SELECT * FROM ml_models ORDER BY id DESC")
        return [self._row_to_meta(r) for r in rows]

    def update_after_training(
        self, model_id: int, version: str, file_path: str, sha256_hash: str,
        training_data_hash: str, features: List[str], metrics: Dict[str, Any],
    ) -> None:
        """Fill in artefact fields once training has actually produced a file."""

        self.db.execute_with_retry(
            """UPDATE ml_models
               SET version = ?, file_path = ?, sha256_hash = ?,
                   training_data_hash = ?, features = ?, metrics = ?
               WHERE id = ?""",
            (
                version, file_path, sha256_hash, training_data_hash,
                json.dumps(features), json.dumps(metrics), model_id,
            ),
        )

    def set_active(self, model_id: int, model_type: str) -> None:
        """Activate one model and deactivate all other models of the same type."""

        self.db.execute_with_retry(
            "UPDATE ml_models SET is_active = 0 WHERE model_type = ?", (model_type,)
        )
        self.db.execute_with_retry(
            "UPDATE ml_models SET is_active = 1 WHERE id = ?", (model_id,)
        )

    @staticmethod
    def _row_to_meta(row) -> ModelMetadata:
        return ModelMetadata(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            model_type=row["model_type"],
            file_path=row["file_path"],
            sha256_hash=row["sha256_hash"],
            training_data_hash=row["training_data_hash"],
            features=json.loads(row["features"]) if row["features"] else [],
            parameters=json.loads(row["parameters"]) if row["parameters"] else {},
            metrics=json.loads(row["metrics"]) if row["metrics"] else {},
            created_at=row["created_at"],
            created_by=row["created_by"],
            is_active=bool(row["is_active"]),
        )
