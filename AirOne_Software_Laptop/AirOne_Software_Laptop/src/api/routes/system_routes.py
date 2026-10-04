"""System endpoints: events, logs, and metrics."""
from __future__ import annotations

import logging
import os

from flask import Blueprint, current_app, request
from marshmallow import ValidationError

from ...security.rbac import Role, require_role
from ..middleware import envelope, jwt_required
from ..schemas import EventsQuerySchema, LogsQuerySchema, validate_request

logger = logging.getLogger(__name__)
bp = Blueprint("system", __name__, url_prefix="/api/v1")

try:
    import psutil  # type: ignore

    _PSUTIL = True
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore
    _PSUTIL = False


def _services():
    return current_app.config["SERVICES"]


@bp.get("/events")
@jwt_required
@require_role(Role.VIEWER)
def events():
    try:
        q = validate_request(EventsQuerySchema(), request.args.to_dict())
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    rows = _services().event_repo.get_recent(q["limit"], q["severity"])
    return envelope(True, data={"events": [e.to_dict() for e in rows], "count": len(rows)})


@bp.get("/system/logs")
@jwt_required
@require_role(Role.ENGINEER)
def system_logs():
    try:
        q = validate_request(LogsQuerySchema(), request.args.to_dict())
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    n = q["n"]
    path = _services().log_path
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-n:]
    return envelope(True, data={"lines": [l.rstrip("\n") for l in lines], "count": len(lines)})


@bp.get("/system/metrics")
@jwt_required
@require_role(Role.OPERATOR)
def system_metrics():
    svc = _services()
    metrics = {
        "cpu_percent": psutil.cpu_percent(interval=0.1) if _PSUTIL else None,
        "ram_percent": psutil.virtual_memory().percent if _PSUTIL else None,
        "disk_percent": psutil.disk_usage("/").percent if _PSUTIL else None,
        "active_threads": None,
    }
    if svc.metrics_provider is not None:
        try:
            metrics.update(svc.metrics_provider())
        except Exception:  # noqa: BLE001
            logger.exception("metrics_provider failed")
    if _PSUTIL:
        import threading

        metrics["active_threads"] = threading.active_count()
    return envelope(True, data=metrics)
