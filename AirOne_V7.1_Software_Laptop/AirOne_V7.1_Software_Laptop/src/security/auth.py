"""Authentication primitives: bcrypt password hashing and JWT tokens.

Hardening applied (Team AirOne security audit):

* Algorithm is pinned to HS256 on both encode and decode — tokens using
  ``none`` or any asymmetric algorithm are rejected by PyJWT before the
  signature is even considered.
* Every token carries ``iss`` (``airone-v71``) and ``aud`` (``airone-api``)
  claims that are *verified* on decode, so a token minted by another service
  sharing the same secret is not accepted here.
* ``exp``, ``iat``, ``jti``, ``sub`` and ``type`` are **required** claims.
* Access tokens are short-lived (default 15 min); refresh tokens (default 7
  days) are rotated on every use — the consumed refresh ``jti`` is revoked so a
  stolen refresh token can be used at most once.
* Revocations are persisted in the ``revoked_tokens`` table of the mission
  database (via :class:`~src.storage.database.DatabaseManager`) so logout
  survives a process restart. Expired revocations are pruned lazily.
* Error messages returned to callers are generic; the PyJWT detail is only
  logged at DEBUG level.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

import bcrypt
import jwt

from ..core.errors import InvalidTokenError, TokenExpiredError
from .config import get_jwt_secret

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
ISSUER = "airone-v71"
AUDIENCE = "airone-api"
REQUIRED_CLAIMS = ("exp", "iat", "jti", "sub", "type", "iss", "aud")

# Hard upper bounds so a misconfiguration can never mint effectively
# immortal tokens.
MAX_ACCESS_MINUTES = 24 * 60
MAX_REFRESH_DAYS = 30


# --- Passwords -------------------------------------------------------------
def hash_password(password: str, rounds: int = 12) -> str:
    """bcrypt hash with a fresh per-user salt (cost factor ``rounds``)."""

    if not password:
        raise ValueError("password must be non-empty")
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time bcrypt verification; ``False`` on any malformed input."""

    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --- Token blacklist -------------------------------------------------------
class TokenBlacklist:
    """Revoked-``jti`` set with optional persistence in the mission database.

    ``db`` is a :class:`~src.storage.database.DatabaseManager` (already
    migrated, so the ``revoked_tokens`` table exists). Without ``db`` the
    blacklist is in-memory only — used for unit tests and clearly logged.
    """

    def __init__(self, db: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._revoked: Set[str] = set()
        self._db = db
        if db is not None:
            self._load()
        else:
            logger.debug("TokenBlacklist running in-memory (no database) — revocations "
                         "will not survive a restart")

    @property
    def persistent(self) -> bool:
        return self._db is not None

    def _load(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # Prune entries whose token has expired anyway (they can never validate).
        self._db.execute_with_retry(
            "DELETE FROM revoked_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        rows = self._db.query("SELECT jti FROM revoked_tokens")
        with self._lock:
            self._revoked = {r["jti"] for r in rows}
        logger.info("TokenBlacklist loaded %d persisted revocation(s)", len(self._revoked))

    def revoke(self, jti: str, expires_at: Optional[datetime] = None) -> None:
        if not jti:
            return
        with self._lock:
            self._revoked.add(jti)
        if self._db is not None:
            self._db.execute_with_retry(
                "INSERT OR IGNORE INTO revoked_tokens (jti, revoked_at, expires_at) "
                "VALUES (?, ?, ?)",
                (
                    jti,
                    datetime.now(timezone.utc).isoformat(),
                    expires_at.astimezone(timezone.utc).isoformat() if expires_at else None,
                ),
            )

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            return jti in self._revoked

    def __len__(self) -> int:
        with self._lock:
            return len(self._revoked)


# Process-wide default blacklist. ``build_services`` replaces it with a
# database-backed instance via :func:`set_default_blacklist`.
_DEFAULT_BLACKLIST = TokenBlacklist()
_BLACKLIST_LOCK = threading.Lock()


def get_blacklist() -> TokenBlacklist:
    with _BLACKLIST_LOCK:
        return _DEFAULT_BLACKLIST


def set_default_blacklist(blacklist: TokenBlacklist) -> None:
    """Install the process-wide blacklist used by :func:`decode_token`."""

    global _DEFAULT_BLACKLIST
    with _BLACKLIST_LOCK:
        _DEFAULT_BLACKLIST = blacklist


# --- Token creation / decoding --------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _base_claims(user_id: str, token_type: str, now: datetime, exp: datetime) -> Dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": uuid.uuid4().hex,
    }


def create_access_token(
    user_id: str,
    role: str,
    expires_minutes: int = 15,
    secret: Optional[str] = None,
) -> str:
    secret = secret or get_jwt_secret()
    if expires_minutes > MAX_ACCESS_MINUTES:
        raise ValueError(f"access token lifetime capped at {MAX_ACCESS_MINUTES} minutes")
    now = _now()
    payload = _base_claims(user_id, "access", now, now + timedelta(minutes=expires_minutes))
    payload["role"] = role
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def create_refresh_token(
    user_id: str,
    expires_days: int = 7,
    secret: Optional[str] = None,
) -> str:
    secret = secret or get_jwt_secret()
    if expires_days > MAX_REFRESH_DAYS:
        raise ValueError(f"refresh token lifetime capped at {MAX_REFRESH_DAYS} days")
    now = _now()
    payload = _base_claims(user_id, "refresh", now, now + timedelta(days=expires_days))
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(
    token: str,
    secret: Optional[str] = None,
    blacklist: Optional[TokenBlacklist] = None,
    expected_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Decode and verify a JWT.

    Verifies signature (HS256 only), ``exp``/``iat``, ``iss``, ``aud`` and the
    presence of all :data:`REQUIRED_CLAIMS`. Raises :class:`TokenExpiredError`
    for expired tokens and :class:`InvalidTokenError` (with a *generic*
    message) for anything else, including revoked tokens and a mismatching
    ``expected_type``.
    """

    secret = secret or get_jwt_secret()
    # ``is None`` — an empty blacklist is falsy because it defines __len__.
    blacklist = blacklist if blacklist is not None else get_blacklist()
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise InvalidTokenError("Invalid token")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        logger.debug("JWT rejected: %s", exc)
        raise InvalidTokenError("Invalid token") from exc
    if payload.get("type") not in ("access", "refresh"):
        raise InvalidTokenError("Invalid token")
    if expected_type is not None and payload.get("type") != expected_type:
        raise InvalidTokenError("Invalid token")
    if blacklist.is_revoked(payload["jti"]):
        raise InvalidTokenError("Token has been revoked")
    return payload


def revoke_token(
    jti: str,
    blacklist: Optional[TokenBlacklist] = None,
    expires_at: Optional[datetime] = None,
) -> None:
    target = blacklist if blacklist is not None else get_blacklist()
    target.revoke(jti, expires_at=expires_at)


def revoke_payload(payload: Dict[str, Any], blacklist: Optional[TokenBlacklist] = None) -> None:
    """Revoke a decoded token, recording its expiry so the entry can be pruned."""

    exp = payload.get("exp")
    expires_at = datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    revoke_token(payload.get("jti", ""), blacklist=blacklist, expires_at=expires_at)
