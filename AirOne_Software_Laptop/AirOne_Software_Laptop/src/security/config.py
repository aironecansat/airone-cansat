"""Security configuration and startup secret enforcement.

The JWT signing secret is loaded from the ``AIRONE_JWT_SECRET`` environment
variable. A production deployment MUST supply a strong secret. The default
placeholder ``ChangeThisSecret`` (or any secret shorter than 32 characters) is
rejected unless ``AIRONE_ALLOW_DEFAULT_SECRET=true`` is set for local
development, in which case a prominent warning is logged.

Validation runs at import time so that an insecure deployment fails fast.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..core.errors import InsecureConfigError

logger = logging.getLogger(__name__)

DEFAULT_SECRET = "ChangeThisSecret"
MIN_SECRET_LENGTH = 32
ENV_SECRET = "AIRONE_JWT_SECRET"
ENV_ALLOW_DEFAULT = "AIRONE_ALLOW_DEFAULT_SECRET"

_INSECURE_MESSAGE = (
    "Production JWT secret is not configured. "
    "Set AIRONE_JWT_SECRET environment variable."
)


def _allow_default() -> bool:
    return os.environ.get(ENV_ALLOW_DEFAULT, "").lower() in ("1", "true", "yes")


def validate_jwt_secret(secret: str) -> str:
    """Validate a JWT secret. Raises :class:`InsecureConfigError` if unsafe.

    When ``AIRONE_ALLOW_DEFAULT_SECRET`` is truthy, an insecure secret is
    permitted for development but a loud warning is logged.
    """

    insecure = secret == DEFAULT_SECRET or len(secret) < MIN_SECRET_LENGTH
    if insecure:
        if _allow_default():
            logger.warning(
                "=" * 70
                + "\nINSECURE JWT SECRET IN USE (development override enabled).\n"
                "Do NOT run this configuration in production.\n" + "=" * 70
            )
            return secret
        raise InsecureConfigError(_INSECURE_MESSAGE)
    return secret


def get_jwt_secret() -> str:
    """Return the validated JWT secret from the environment."""

    secret = os.environ.get(ENV_SECRET, DEFAULT_SECRET)
    return validate_jwt_secret(secret)


@dataclass
class SecurityConfig:
    jwt_secret: str
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    bcrypt_rounds: int = 12
    max_login_attempts: int = 5
    lockout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "SecurityConfig":
        return cls(
            jwt_secret=get_jwt_secret(),
            access_token_minutes=int(os.environ.get("AIRONE_ACCESS_MINUTES", "15")),
            refresh_token_days=int(os.environ.get("AIRONE_REFRESH_DAYS", "7")),
            bcrypt_rounds=int(os.environ.get("AIRONE_BCRYPT_ROUNDS", "12")),
        )


# --- Import-time enforcement -----------------------------------------------
# If a secret is explicitly configured in the environment, validate it now so
# an insecure deployment fails at startup rather than at first login. If no
# secret is set at all we defer (the launcher performs the authoritative
# check) to keep unit-test imports lightweight.
if os.environ.get(ENV_SECRET) is not None:
    validate_jwt_secret(os.environ[ENV_SECRET])
