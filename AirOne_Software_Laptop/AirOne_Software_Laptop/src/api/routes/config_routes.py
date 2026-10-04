"""Configuration endpoints."""
from __future__ import annotations

import copy
import logging

from flask import Blueprint, current_app, g, request
from marshmallow import ValidationError

from ...security.audit import AuditEvent, get_audit_logger
from ...security.rbac import Role, require_role
from ..middleware import envelope, jwt_required
from ..schemas import ConfigUpdateSchema, validate_request

logger = logging.getLogger(__name__)
bp = Blueprint("config", __name__, url_prefix="/api/v1/config")

# Keys that must never be exposed via the API.
_REDACT = {"jwt_secret", "admin_password", "viewer_password", "link_key", "secret"}

# Runtime-tunable top-level keys. Anything else (api bind address, storage
# paths, security settings, ...) is deliberately NOT changeable over HTTP —
# those require an operator editing the config file and restarting.
_MUTABLE_KEYS = {
    "mission_id", "objectives", "default_primary", "default_secondary",
    "pipeline", "scientific", "ml", "gui", "simulation", "logging",
}


def _services():
    return current_app.config["SERVICES"]


def _redacted(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for k in list(out.keys()):
        if k in _REDACT:
            out[k] = "***"
    return out


@bp.get("")
@jwt_required
@require_role(Role.ENGINEER)
def get_config():
    return envelope(True, data=_redacted(_services().config))


@bp.put("")
@jwt_required
@require_role(Role.ENGINEER)
def update_config():
    try:
        body = validate_request(ConfigUpdateSchema(), request.get_json(silent=True) or {})
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    svc = _services()
    rejected = sorted(k for k in body["config"] if k not in _MUTABLE_KEYS or k in _REDACT)
    if rejected:
        return envelope(
            False,
            error=f"Keys not changeable at runtime: {rejected}",
            data={"mutable_keys": sorted(_MUTABLE_KEYS)},
            status=400,
        )
    updates = dict(body["config"])
    before = _redacted(svc.config)
    svc.config.update(updates)
    get_audit_logger().log(
        AuditEvent.CONFIG_CHANGE, user_id=g.user_id, role=g.role.name,
        details={"changed_keys": list(updates.keys()), "before": before},
        result="success", request_ip=request.remote_addr, endpoint="/config",
    )
    return envelope(True, data=_redacted(svc.config))


@bp.post("/validate")
@jwt_required
@require_role(Role.ENGINEER)
def validate_config():
    try:
        body = validate_request(ConfigUpdateSchema(), request.get_json(silent=True) or {})
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    cfg = body["config"]
    errors = []
    if "api" in cfg and not isinstance(cfg["api"], dict):
        errors.append("api must be an object")
    port = cfg.get("port")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
        errors.append("port must be an integer in 1-65535")
    api = cfg.get("api")
    if isinstance(api, dict):
        host = api.get("host")
        if host is not None and host not in ("127.0.0.1", "localhost", "::1") and host == "0.0.0.0":
            errors.append("api.host 0.0.0.0 exposes the API on every interface; bind to 127.0.0.1 unless a TLS reverse proxy fronts it")
    return envelope(True, data={"valid": not errors, "errors": errors})
