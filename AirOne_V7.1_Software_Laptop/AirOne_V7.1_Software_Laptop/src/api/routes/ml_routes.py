"""ML model registry, training, and advisory anomaly endpoints.

The ML tier is *advisory only*. Nothing in this module can change mission
state; the endpoints surface anomaly scores, drift metrics, and model
provenance, and every response states honestly whether the tier is available.
Training runs synchronously over persisted telemetry and produces a checksummed
model artefact whose SHA-256 is recorded in the registry.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, request
from marshmallow import ValidationError

from ...ml import available_detectors as ml_available_detectors
from ...ml.registry import ModelStore, SafeLoadError
from ...ml.trainer import DETECTOR_TYPES, train_detector
from ...security.audit import AuditEvent, get_audit_logger
from ...security.rbac import Role, require_role
from ...storage.repositories import ModelMetadata
from ..middleware import envelope, jwt_required
from ..schemas import TrainSchema, validate_request
from .analysis_routes import _frames_from_storage

logger = logging.getLogger(__name__)
bp = Blueprint("ml", __name__, url_prefix="/api/v1/ml")


def _services():
    return current_app.config["SERVICES"]


def _model_store(svc) -> ModelStore:
    directory = (svc.config.get("ml", {}) or {}).get("model_dir", "data/models")
    return ModelStore(directory=directory)


@bp.get("/detectors")
@jwt_required
@require_role(Role.SCIENTIST)
def detectors():
    """Report which detectors are available and why (honest dependency probe)."""
    return envelope(True, data={"detectors": ml_available_detectors()})


@bp.get("/models")
@jwt_required
@require_role(Role.SCIENTIST)
def list_models():
    models = _services().ml_repo.list_all()
    return envelope(True, data={"models": [m.__dict__ for m in models]})


@bp.get("/models/<int:model_id>")
@jwt_required
@require_role(Role.SCIENTIST)
def model_details(model_id: int):
    model = _services().ml_repo.get_by_id(model_id)
    if model is None:
        return envelope(False, error="Model not found", status=404)
    return envelope(True, data=model.__dict__)


@bp.get("/anomalies")
@jwt_required
@require_role(Role.SCIENTIST)
def anomalies():
    """Latest advisory anomaly summary from the live ML worker.

    Returns 503 with an explicit NOT_CONFIGURED state when the ML tier is not
    running, rather than fabricating an empty/clean result.
    """
    worker = getattr(_services(), "ml_worker", None)
    if worker is None:
        return envelope(
            False, error="ML analysis tier is not running",
            data={"state": "NOT_CONFIGURED"}, status=503,
        )
    summary = worker.latest_summary()
    summary.setdefault(
        "advisory_note",
        "ML output is advisory only and cannot change mission state.",
    )
    return envelope(True, data=summary)


@bp.get("/drift")
@jwt_required
@require_role(Role.SCIENTIST)
def drift():
    """Latest population-stability drift metrics from the live ML worker."""
    worker = getattr(_services(), "ml_worker", None)
    if worker is None:
        return envelope(
            False, error="ML analysis tier is not running",
            data={"state": "NOT_CONFIGURED"}, status=503,
        )
    summary = worker.latest_summary()
    return envelope(True, data={
        "state": summary.get("state", "UNKNOWN"),
        "drift": summary.get("drift", []),
        "reference_set": bool(summary.get("drift")),
    })


@bp.post("/train")
@jwt_required
@require_role(Role.SCIENTIST)
def train():
    try:
        body = validate_request(TrainSchema(), request.get_json(silent=True) or {})
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)

    model_type = body["model_type"]
    if model_type not in DETECTOR_TYPES:
        return envelope(
            False,
            error=(f"Unknown model_type '{model_type}'. "
                   f"Available: {sorted(DETECTOR_TYPES)}"),
            status=400,
        )

    svc = _services()
    mission_id = str(svc.config.get("mission_id", "default"))
    params = body.get("parameters", {}) or {}
    limit = int(params.get("limit", 5000))
    frames = _frames_from_storage(svc, mission_id, limit)
    if not frames:
        return envelope(
            False, error="No stored telemetry to train on",
            data={"state": "UNAVAILABLE"}, status=422,
        )

    features = params.get("features")
    store = _model_store(svc)
    outcome = train_detector(
        frames, model_type=model_type, features=features, store=store,
        params={k: v for k, v in params.items()
                if k not in ("limit", "features")},
    )

    if not outcome.ok:
        get_audit_logger().log(
            AuditEvent.MODEL_TRAIN, user_id=g.user_id, role=g.role.name,
            details={"model_type": model_type, "reason": outcome.reason},
            result="failed", request_ip=request.remote_addr, endpoint="/ml/train",
        )
        # 422: request was well-formed but training could not honestly proceed.
        return envelope(False, error=outcome.reason,
                        data=outcome.to_dict(), status=422)

    # Persist provenance to the registry and activate the new model.
    meta = ModelMetadata(
        name=body.get("name", model_type), version=outcome.version,
        model_type=model_type, parameters=params, created_by=g.user_id,
        is_active=False,
    )
    model_id = svc.ml_repo.register(meta)
    svc.ml_repo.update_after_training(
        model_id=model_id, version=outcome.version, file_path=outcome.file_path,
        sha256_hash=outcome.sha256_hash, training_data_hash=outcome.training_data_hash,
        features=outcome.features, metrics=outcome.metrics,
    )
    svc.ml_repo.set_active(model_id, model_type)

    get_audit_logger().log(
        AuditEvent.MODEL_TRAIN, user_id=g.user_id, role=g.role.name,
        details={"model_id": model_id, "model_type": model_type,
                 "sha256": outcome.sha256_hash, "n_samples": outcome.n_samples},
        result="trained", request_ip=request.remote_addr, endpoint="/ml/train",
    )
    data = outcome.to_dict()
    data.update({"model_id": model_id, "status": "trained"})
    return envelope(True, data=data)
