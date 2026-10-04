"""User management endpoints (ADMIN only, permission ``user_management``).

Passwords are never returned; every mutation is audited.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, request
from marshmallow import ValidationError

from ...security.audit import AuditEvent, get_audit_logger
from ...security.rbac import Role, require_permission
from ..middleware import envelope, jwt_required
from ..schemas import CreateUserSchema, validate_request

logger = logging.getLogger(__name__)
bp = Blueprint("users", __name__, url_prefix="/api/v1/users")


def _services():
    return current_app.config["SERVICES"]


def _store():
    store = _services().user_store
    if store is None:
        raise RuntimeError("user store not configured")
    return store


@bp.get("")
@jwt_required
@require_permission("user_management")
def list_users():
    return envelope(True, data={"users": [u.to_public_dict() for u in _store().list_all()]})


@bp.post("")
@jwt_required
@require_permission("user_management")
def create_user():
    try:
        data = validate_request(CreateUserSchema(), request.get_json(silent=True))
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    try:
        record = _store().create(
            data["username"], data["password"], Role.from_name(data["role"]),
            must_change_password=data["must_change_password"],
        )
    except ValueError as exc:
        # Duplicate user or weak password — message contains no secrets.
        return envelope(False, error=str(exc), status=400)
    get_audit_logger().log(
        AuditEvent.USER_CREATE, user_id=g.user_id, role=g.role.name,
        details={"created": record.username, "created_role": record.role},
        result="success", request_ip=request.remote_addr, endpoint="/users",
    )
    return envelope(True, data=record.to_public_dict(), status=201)


@bp.delete("/<username>")
@jwt_required
@require_permission("user_management")
def delete_user(username: str):
    store = _store()
    if username == g.user_id:
        return envelope(False, error="Cannot delete the account you are logged in with", status=400)
    if store.get(username) is None:
        return envelope(False, error="Not found", status=404)
    store.delete(username)
    get_audit_logger().log(
        AuditEvent.USER_DELETE, user_id=g.user_id, role=g.role.name,
        details={"deleted": username}, result="success",
        request_ip=request.remote_addr, endpoint="/users",
    )
    return envelope(True, data={"deleted": username})


@bp.post("/<username>/disable")
@jwt_required
@require_permission("user_management")
def disable_user(username: str):
    return _set_disabled(username, True)


@bp.post("/<username>/enable")
@jwt_required
@require_permission("user_management")
def enable_user(username: str):
    return _set_disabled(username, False)


def _set_disabled(username: str, disabled: bool):
    store = _store()
    if disabled and username == g.user_id:
        return envelope(False, error="Cannot disable the account you are logged in with", status=400)
    if store.get(username) is None:
        return envelope(False, error="Not found", status=404)
    store.set_disabled(username, disabled)
    get_audit_logger().log(
        AuditEvent.USER_DISABLE if disabled else AuditEvent.USER_ENABLE,
        user_id=g.user_id, role=g.role.name, details={"target": username},
        result="success", request_ip=request.remote_addr, endpoint="/users",
    )
    record = store.get(username)
    return envelope(True, data=record.to_public_dict() if record else {"username": username})
