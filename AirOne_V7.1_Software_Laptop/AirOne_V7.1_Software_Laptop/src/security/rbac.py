"""Role-based access control with an explicit permission matrix."""
from __future__ import annotations

import functools
import logging
from enum import IntEnum
from typing import Tuple, Callable, Dict

from ..core.errors import PermissionDeniedError

logger = logging.getLogger(__name__)


class Role(IntEnum):
    VIEWER = 0
    OPERATOR = 1
    SCIENTIST = 2
    ENGINEER = 3
    ADMIN = 4

    @classmethod
    def from_name(cls, name: str) -> "Role":
        try:
            return cls[name.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown role '{name}'") from exc


ROLE_NAMES: Tuple[str, ...] = tuple(r.name for r in Role)


# Explicit permission -> minimum role required.
PERMISSION_MATRIX: Dict[str, Role] = {
    "read_telemetry": Role.VIEWER,
    "export_data": Role.OPERATOR,
    "train_ml_model": Role.SCIENTIST,
    "delete_data": Role.ENGINEER,
    "system_config": Role.ENGINEER,
    "user_management": Role.ADMIN,
    "system_shutdown": Role.ADMIN,
}


def role_has_permission(role: Role, permission: str) -> bool:
    if permission not in PERMISSION_MATRIX:
        raise KeyError(f"Unknown permission '{permission}'")
    return int(role) >= int(PERMISSION_MATRIX[permission])


def check_permission(role: Role, permission: str) -> None:
    if not role_has_permission(role, permission):
        raise PermissionDeniedError(
            f"Role {role.name} lacks permission '{permission}' "
            f"(requires {PERMISSION_MATRIX[permission].name})"
        )


# --- Flask decorator factories ---------------------------------------------
def _extract_role() -> Role:
    """Read the authenticated role from Flask's request context."""

    from flask import g  # local import so module is usable without Flask

    role = getattr(g, "role", None)
    if role is None:
        raise PermissionDeniedError("No authenticated role in request context")
    if isinstance(role, Role):
        return role
    return Role.from_name(str(role))


def require_permission(permission: str) -> Callable:
    """Flask decorator: require ``permission`` for the wrapped view."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import jsonify

            try:
                role = _extract_role()
                check_permission(role, permission)
            except PermissionDeniedError as exc:
                logger.warning("Permission denied: %s", exc)
                return jsonify({
                    "success": False,
                    "data": None,
                    "error": str(exc),
                }), 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def require_role(minimum_role: Role) -> Callable:
    """Flask decorator: require at least ``minimum_role``."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import jsonify

            try:
                role = _extract_role()
            except PermissionDeniedError as exc:
                return jsonify({"success": False, "data": None, "error": str(exc)}), 401
            if int(role) < int(minimum_role):
                return jsonify({
                    "success": False,
                    "data": None,
                    "error": f"Requires role {minimum_role.name}, have {role.name}",
                }), 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator
