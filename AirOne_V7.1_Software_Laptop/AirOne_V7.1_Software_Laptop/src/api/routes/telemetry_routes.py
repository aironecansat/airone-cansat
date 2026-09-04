"""Telemetry and status endpoints."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, request
from marshmallow import ValidationError

from ...security.audit import AuditEvent, get_audit_logger
from ...security.rbac import Role, require_role
from ..middleware import envelope, jwt_required
from ..schemas import ExportSchema, HistoryQuerySchema, validate_request

logger = logging.getLogger(__name__)
bp = Blueprint("telemetry", __name__, url_prefix="/api/v1")

# Characters permitted in a sensor id that becomes part of an export filename.
_SAFE_FILENAME_PART = re.compile(r"^[A-Za-z0-9_.\-:]{1,64}$")


def _services():
    return current_app.config["SERVICES"]


# --- status (public) --------------------------------------------------------
@bp.get("/status")
def status():
    """Public liveness probe. Deliberately minimal: no versions, no paths.

    ``link_state`` is the *real* telemetry link status reported by the
    orchestrator (``NOT_CONFIGURED`` when no transport is attached) — it is
    never hard-coded to "connected".
    """

    svc = _services()
    link_state = "NOT_CONFIGURED"
    provider = getattr(svc, "telemetry_link_status", None)
    if callable(provider):
        try:
            link_state = str(provider())
        except Exception:  # noqa: BLE001
            link_state = "UNAVAILABLE"
    return envelope(True, data={
        "mission_state": svc.mission_machine.state.value,
        "link_state": link_state,
        "connected": link_state == "CONNECTED",
        "component_health": {"api": "ok", "database": svc.db.health_check().connected},
    })


@bp.get("/health")
@jwt_required
@require_role(Role.VIEWER)
def health():
    svc = _services()
    dbh = svc.db.health_check()
    return envelope(True, data={
        "database": {
            "connected": dbh.connected,
            "size_mb": dbh.size_mb,
            "wal_size_mb": dbh.wal_size_mb,
            "table_counts": dbh.table_counts,
        },
        "mission_state": svc.mission_machine.state.value,
    })


# --- telemetry --------------------------------------------------------------
@bp.get("/telemetry/latest")
@jwt_required
@require_role(Role.VIEWER)
def latest():
    svc = _services()
    measurements = svc.telemetry_repo.latest_per_sensor()
    return envelope(True, data={"measurements": [m.to_dict() for m in measurements]})


@bp.get("/telemetry/history")
@jwt_required
@require_role(Role.VIEWER)
def history():
    try:
        q = validate_request(HistoryQuerySchema(), request.args.to_dict())
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    svc = _services()
    rows = svc.telemetry_repo.get_history(
        q["sensor_id"], q["start"], q["end"], q["quality"], q["limit"]
    )
    return envelope(True, data={"measurements": [m.to_dict() for m in rows], "count": len(rows)})


@bp.post("/telemetry/export")
@jwt_required
@require_role(Role.OPERATOR)
def export():
    try:
        body = validate_request(ExportSchema(), request.get_json(silent=True) or {})
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    svc = _services()
    sensor_id = body["sensor_id"] or "fused_pressure"
    # Defence in depth: the schema already restricts sensor_id to a safe
    # character set, but the value is used in a filename so re-check here.
    if not _SAFE_FILENAME_PART.match(sensor_id):
        return envelope(False, error="Validation error: invalid sensor_id", status=400)
    rows = svc.telemetry_repo.get_history(sensor_id, body["start"], body["end"], None, 100000)
    export_dir = os.path.abspath(svc.config.get("export_dir", "data/exports"))
    os.makedirs(export_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if body["format"] == "json":
        path = os.path.join(export_dir, f"export_{sensor_id}_{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([m.to_dict() for m in rows], fh, indent=2)
    else:
        path = os.path.join(export_dir, f"export_{sensor_id}_{stamp}.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "sensor_id", "field_name", "value", "unit", "timestamp",
                "quality", "valid", "uncertainty", "source",
            ])
            for m in rows:
                writer.writerow([
                    m.sensor_id, m.field_name, m.value, m.unit,
                    m.timestamp.isoformat(), m.quality.value, m.valid,
                    m.uncertainty, m.source.value,
                ])
    # The response exposes only the file name, never the server-side path.
    filename = os.path.basename(path)
    get_audit_logger().log(
        AuditEvent.DATA_EXPORT, user_id=g.user_id, role=g.role.name,
        details={"file": filename, "count": len(rows)}, result="success",
        request_ip=request.remote_addr, endpoint="/telemetry/export",
    )
    return envelope(True, data={"file": filename, "count": len(rows), "format": body["format"]})
