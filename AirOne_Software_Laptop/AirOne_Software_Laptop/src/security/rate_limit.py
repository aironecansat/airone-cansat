"""Per-endpoint rate limiting built on flask-limiter.

If flask-limiter is unavailable the module still imports and exposes the limit
definitions so configuration remains inspectable; limiting is then a no-op with
a clear warning.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Explicit per-endpoint limits.
ENDPOINT_LIMITS: Dict[str, str] = {
    "auth_login": "10 per minute",
    "auth_refresh": "30 per minute",
    "telemetry_export": "5 per minute",
    "ml_train": "2 per hour",
    "config": "20 per minute",
}

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    _LIMITER_AVAILABLE = True
except Exception:  # noqa: BLE001
    Limiter = None  # type: ignore
    get_remote_address = None  # type: ignore
    _LIMITER_AVAILABLE = False
    logger.warning("flask-limiter unavailable: API rate limiting is DISABLED")


def limiter_available() -> bool:
    return _LIMITER_AVAILABLE


def _key_func():
    """Rate-limit key: authenticated user id if present, else remote IP."""

    from flask import g

    user_id = getattr(g, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return get_remote_address() if get_remote_address else "global"


def create_limiter(app=None, default_limits: Optional[list] = None):
    """Create and (optionally) attach a Limiter to a Flask app."""

    if not _LIMITER_AVAILABLE:
        return None
    limiter = Limiter(
        key_func=_key_func,
        default_limits=default_limits or ["200 per minute"],
        storage_uri="memory://",
    )
    if app is not None:
        limiter.init_app(app)
    return limiter
