"""Authentication endpoints: login, refresh (rotating), logout, password change.

Every attempt — success or failure, throttled or not — is written to the
tamper-evident audit log. Failure responses are deliberately generic
(``Invalid credentials``) so an attacker cannot enumerate usernames; the exact
reason is only in the audit log.

Explicit states returned to clients:

* ``PASSWORD_CHANGE_REQUIRED`` (403) — account (typically a seeded demo
  account) must change its password via ``POST /auth/change-password`` before
  it can obtain tokens.
* ``ACCOUNT_LOCKED`` / ``IP_LOCKED`` (429) — brute-force protection tripped;
  ``retry_after_s`` says when to try again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, request
from marshmallow import ValidationError

from ...core.errors import InvalidTokenError, TokenExpiredError
from ...security.audit import AuditEvent, get_audit_logger
from ...security.auth import (
    MAX_ACCESS_MINUTES,
    MAX_REFRESH_DAYS,
    create_access_token,
    create_refresh_token,
    decode_token,
    revoke_payload,
    revoke_token,
)
from ..middleware import envelope, jwt_required
from ..schemas import (
    ChangePasswordSchema,
    LoginSchema,
    RefreshSchema,
    SetOwnPasswordSchema,
    validate_request,
)

logger = logging.getLogger(__name__)
bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")

DEFAULT_ACCESS_MINUTES = 15
DEFAULT_REFRESH_DAYS = 7


def _services():
    return current_app.config["SERVICES"]


def _security_cfg() -> dict:
    sec = _services().config.get("security", {})
    return sec if isinstance(sec, dict) else {}


def _access_minutes() -> int:
    cfg = _security_cfg()
    # ``jwt_expiry_minutes`` is the alternative key name from default_config.yaml.
    value = cfg.get("access_token_minutes", cfg.get("jwt_expiry_minutes", DEFAULT_ACCESS_MINUTES))
    return max(1, min(int(value), MAX_ACCESS_MINUTES))


def _refresh_days() -> int:
    cfg = _security_cfg()
    value = cfg.get("refresh_token_days", cfg.get("refresh_expiry_days", DEFAULT_REFRESH_DAYS))
    return max(1, min(int(value), MAX_REFRESH_DAYS))


def _client_ip() -> str:
    # The API is meant to sit on localhost or behind a trusted reverse proxy;
    # X-Forwarded-For is *not* trusted here because it is client-controlled.
    return request.remote_addr or "unknown"


def _issue_tokens(user_id: str, role: str) -> dict:
    minutes = _access_minutes()
    return {
        "access_token": create_access_token(user_id, role, expires_minutes=minutes),
        "refresh_token": create_refresh_token(user_id, expires_days=_refresh_days()),
        "token_type": "Bearer",
        "expires_in": minutes * 60,
        "role": role,
    }


def _throttled_response(decision, audit, username: str, endpoint: str):
    audit.log(
        AuditEvent.ACCOUNT_LOCKED, user_id=username, result="blocked",
        details={"reason": decision.reason, "retry_after_s": decision.retry_after_s},
        request_ip=_client_ip(), endpoint=endpoint,
    )
    resp, status = envelope(
        False, error="Too many failed attempts",
        data={"state": decision.reason, "retry_after_s": decision.retry_after_s},
        status=429,
    )
    resp.headers["Retry-After"] = str(decision.retry_after_s)
    return resp, status


@bp.post("/login")
def login():
    audit = get_audit_logger()
    svc = _services()
    try:
        data = validate_request(LoginSchema(), request.get_json(silent=True))
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    username, ip = data["username"], _client_ip()

    decision = svc.throttle.check(username, ip)
    if not decision.allowed:
        return _throttled_response(decision, audit, username, "/auth/login")

    result = svc.authenticate(username, data["password"])
    if not result.ok:
        lock = svc.throttle.record_failure(username, ip)
        audit.log(
            AuditEvent.LOGIN_FAILURE, user_id=username, result="failure",
            details={"reason": result.state, "locked": not lock.allowed},
            request_ip=ip, endpoint="/auth/login",
        )
        if not lock.allowed:
            return _throttled_response(lock, audit, username, "/auth/login")
        return envelope(False, error="Invalid credentials", status=401)

    user = result.user
    svc.throttle.record_success(username)
    if user.must_change_password:
        audit.log(
            AuditEvent.LOGIN_FAILURE, user_id=username, role=user.role, result="password_change_required",
            request_ip=ip, endpoint="/auth/login",
        )
        return envelope(
            False, error="Password change required before login",
            data={"state": "PASSWORD_CHANGE_REQUIRED",
                  "change_endpoint": "/api/v1/auth/change-password"},
            status=403,
        )
    tokens = _issue_tokens(user.username, user.role)
    audit.log(
        AuditEvent.LOGIN_SUCCESS, user_id=user.username, role=user.role,
        result="success", request_ip=ip, endpoint="/auth/login",
    )
    return envelope(True, data=tokens)


@bp.post("/refresh")
def refresh():
    """Exchange a refresh token for a new pair. The presented refresh token is
    revoked (rotation) so replaying it fails."""

    svc = _services()
    audit = get_audit_logger()
    try:
        data = validate_request(RefreshSchema(), request.get_json(silent=True))
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    try:
        payload = decode_token(data["refresh_token"], expected_type="refresh")
    except TokenExpiredError:
        return envelope(False, error="Refresh token expired", status=401)
    except InvalidTokenError:
        audit.log(AuditEvent.SECURITY_VIOLATION, result="invalid_refresh_token",
                  request_ip=_client_ip(), endpoint="/auth/refresh")
        return envelope(False, error="Invalid token", status=401)
    user_id = payload.get("sub")
    user = svc.user_store.get(user_id) if svc.user_store else None
    if user is None or user.disabled or user.must_change_password:
        # Never fall back to a default role: a token for an unknown/disabled
        # account is refused, and the refresh token is burned.
        revoke_payload(payload)
        audit.log(AuditEvent.LOGIN_FAILURE, user_id=user_id, result="refresh_refused",
                  details={"reason": "unknown_or_disabled_user"},
                  request_ip=_client_ip(), endpoint="/auth/refresh")
        return envelope(False, error="Invalid token", status=401)
    revoke_payload(payload)  # rotation: single use
    tokens = _issue_tokens(user.username, user.role)
    audit.log(
        AuditEvent.TOKEN_REFRESH, user_id=user.username, role=user.role,
        result="success", request_ip=_client_ip(), endpoint="/auth/refresh",
    )
    return envelope(True, data=tokens)


@bp.post("/logout")
@jwt_required
def logout():
    """Revoke the presented access token and, optionally, a refresh token
    supplied in the body (``{"refresh_token": "..."}``)."""

    exp = getattr(g, "token_exp", None)
    revoke_token(
        g.jti,
        expires_at=datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None,
    )
    body = request.get_json(silent=True)
    revoked_refresh = False
    if isinstance(body, dict) and isinstance(body.get("refresh_token"), str):
        try:
            rp = decode_token(body["refresh_token"], expected_type="refresh")
            if rp.get("sub") == g.user_id:
                revoke_payload(rp)
                revoked_refresh = True
        except (InvalidTokenError, TokenExpiredError):
            pass
    get_audit_logger().log(
        AuditEvent.LOGOUT, user_id=g.user_id, role=g.role.name,
        details={"refresh_revoked": revoked_refresh},
        result="success", request_ip=_client_ip(), endpoint="/auth/logout",
    )
    return envelope(True, data={"message": "Logged out", "refresh_revoked": revoked_refresh})


@bp.post("/change-password")
def change_password():
    """Unauthenticated (but throttled) password change for accounts flagged
    ``must_change_password``, and for any user who knows their current password."""

    svc = _services()
    audit = get_audit_logger()
    try:
        data = validate_request(ChangePasswordSchema(), request.get_json(silent=True))
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    username, ip = data["username"], _client_ip()
    decision = svc.throttle.check(username, ip)
    if not decision.allowed:
        return _throttled_response(decision, audit, username, "/auth/change-password")
    result = svc.authenticate(username, data["current_password"])
    if not result.ok:
        lock = svc.throttle.record_failure(username, ip)
        audit.log(AuditEvent.LOGIN_FAILURE, user_id=username, result="failure",
                  details={"reason": result.state, "via": "change-password"},
                  request_ip=ip, endpoint="/auth/change-password")
        if not lock.allowed:
            return _throttled_response(lock, audit, username, "/auth/change-password")
        return envelope(False, error="Invalid credentials", status=401)
    return _apply_password_change(svc, audit, username, data["new_password"], ip,
                                  "/auth/change-password", role=result.user.role)


@bp.post("/password")
@jwt_required
def set_own_password():
    """Authenticated password change for the token's subject."""

    svc = _services()
    audit = get_audit_logger()
    try:
        data = validate_request(SetOwnPasswordSchema(), request.get_json(silent=True))
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    result = svc.authenticate(g.user_id, data["current_password"])
    if not result.ok:
        audit.log(AuditEvent.LOGIN_FAILURE, user_id=g.user_id, role=g.role.name, result="failure",
                  details={"reason": result.state, "via": "password"},
                  request_ip=_client_ip(), endpoint="/auth/password")
        return envelope(False, error="Invalid credentials", status=401)
    return _apply_password_change(svc, audit, g.user_id, data["new_password"], _client_ip(),
                                  "/auth/password", role=g.role.name)


def _apply_password_change(svc, audit, username, new_password, ip, endpoint, role=None):
    try:
        svc.user_store.set_password(username, new_password)
    except ValueError as exc:
        # Policy violation: the message lists the concrete problems (no secrets).
        return envelope(False, error=str(exc), data={"state": "WEAK_PASSWORD"}, status=400)
    svc.throttle.record_success(username)
    audit.log(AuditEvent.PASSWORD_CHANGE, user_id=username, role=role, result="success",
              request_ip=ip, endpoint=endpoint)
    return envelope(True, data={"message": "Password changed", "username": username})
