"""Mission state endpoints."""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, request
from marshmallow import ValidationError

from ...core.errors import InvalidTransitionError
from ...core.models import MissionState
from ...security.audit import AuditEvent, get_audit_logger
from ...security.rbac import Role, require_role
from ..middleware import envelope, jwt_required
from ..schemas import MissionStateSchema, validate_request

logger = logging.getLogger(__name__)
bp = Blueprint("mission", __name__, url_prefix="/api/v1/mission")


def _services():
    return current_app.config["SERVICES"]


@bp.get("")
@jwt_required
@require_role(Role.VIEWER)
def mission():
    svc = _services()
    return envelope(True, data={
        "state": svc.mission_machine.state.value,
        "objectives": svc.config.get("objectives", {
            "primary": svc.config.get("default_primary", "atmospheric_profiling"),
            "secondary": svc.config.get("default_secondary", "uv_radiation_mapping"),
        }),
    })


@bp.get("/primary")
@jwt_required
@require_role(Role.VIEWER)
def primary():
    svc = _services()
    return envelope(True, data={
        "name": svc.config.get("default_primary", "atmospheric_profiling"),
        "state": svc.mission_machine.state.value,
        "status": "active",
    })


@bp.get("/secondary")
@jwt_required
@require_role(Role.VIEWER)
def secondary():
    svc = _services()
    return envelope(True, data={
        "name": svc.config.get("default_secondary", "uv_radiation_mapping"),
        "state": svc.mission_machine.state.value,
        "status": "active",
    })


@bp.post("/state")
@jwt_required
@require_role(Role.ENGINEER)
def set_state():
    try:
        body = validate_request(MissionStateSchema(), request.get_json(silent=True) or {})
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    svc = _services()
    try:
        new_state = MissionState(body["new_state"])
    except ValueError:
        return envelope(False, error=f"Unknown state '{body['new_state']}'", status=400)
    try:
        transition = svc.mission_machine.transition(new_state, reason=body["reason"])
    except InvalidTransitionError as exc:
        return envelope(False, error=str(exc), status=409)
    svc.mission_repo.store_transition(transition)
    get_audit_logger().log(
        AuditEvent.MISSION_STATE_CHANGE, user_id=g.user_id, role=g.role.name,
        details={"new_state": new_state.value, "reason": body["reason"]},
        result="success", request_ip=request.remote_addr, endpoint="/mission/state",
    )
    return envelope(True, data=transition.to_dict())
