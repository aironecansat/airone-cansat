"""API middleware: JWT auth, request ids, security headers and JSON envelopes."""
from __future__ import annotations

import functools
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from flask import g, jsonify, request

from ..core.errors import InvalidTokenError, TokenExpiredError
from ..security.auth import decode_token
from ..security.rbac import Role
from ..system_logging import LogContext

logger = logging.getLogger(__name__)

# Security headers applied to every response. The API serves JSON only, so a
# maximally restrictive CSP is safe and blocks any accidental HTML rendering.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

# Paths whose responses must never be cached by browsers or proxies.
NO_STORE_PREFIXES = ("/api/v1/auth/", "/api/v1/users", "/api/v1/config")


def envelope(
    success: bool,
    data: Any = None,
    error: Optional[str] = None,
    status: int = 200,
):
    body = {
        "success": success,
        "data": data,
        "error": error,
        "request_id": getattr(g, "request_id", None),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(body), status


def install_request_context(app) -> None:
    @app.before_request
    def _assign_request_id():  # noqa: ANN202
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        # A client-supplied id is only echoed if it is a short, plain token —
        # never reflect arbitrary bytes into logs/headers.
        if len(rid) > 64 or not rid.replace("-", "").replace("_", "").isalnum():
            rid = uuid.uuid4().hex
        g.request_id = rid
        LogContext.set_request_id(rid)

    @app.after_request
    def _finalise(response):  # noqa: ANN202
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.path.startswith(NO_STORE_PREFIXES) or request.path == "/api/v1/status":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        # Do not advertise the server stack.
        response.headers["Server"] = "AirOne"
        LogContext.clear()
        return response


def jwt_required(fn: Callable) -> Callable:
    """Require a valid *access* token; populate ``g.user_id``, ``g.role``, ``g.jti``.

    A token whose role is unknown is rejected (401) rather than silently
    downgraded — an unrecognised role is a sign of a forged or stale token.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return envelope(False, error="Missing bearer token", status=401)
        token = auth_header[7:].strip()
        try:
            payload = decode_token(token, expected_type="access")
        except TokenExpiredError:
            return envelope(False, error="Token expired", status=401)
        except InvalidTokenError as exc:
            return envelope(False, error=str(exc), status=401)
        try:
            role = Role.from_name(str(payload.get("role", "")))
        except ValueError:
            logger.warning("Access token with unknown role rejected (sub=%s)", payload.get("sub"))
            return envelope(False, error="Invalid token", status=401)
        g.user_id = payload.get("sub")
        g.role = role
        g.jti = payload.get("jti")
        g.token_exp = payload.get("exp")
        return fn(*args, **kwargs)

    return wrapper
