"""Persistent user store backed by the AirOne SQLite database.

Design goals:

* **No shipped credentials.** A fresh database has *no* accounts. The first
  admin is created explicitly (``launcher.py --create-admin``). Demo accounts
  are seeded only when ``AIRONE_ALLOW_DEFAULT_USERS`` is truthy, are flagged
  ``is_default_account`` and ``must_change_password``, and are loudly logged.
* **Strong hashing.** bcrypt with a per-user salt (cost 12 by default).
* **Constant-time behaviour.** Authentication of an unknown username still
  performs a bcrypt verification against a fixed dummy hash so response timing
  does not reveal whether an account exists.
* **Explicit states.** :meth:`UserStore.authenticate` returns an
  :class:`AuthResult` whose ``state`` is one of ``OK``, ``BAD_CREDENTIALS``,
  ``DISABLED`` — never a bare ``None`` that callers might misread.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..storage.database import DatabaseManager
from .auth import hash_password, verify_password
from .passwords import validate_password
from .rbac import Role

logger = logging.getLogger(__name__)

ENV_ALLOW_DEFAULT_USERS = "AIRONE_ALLOW_DEFAULT_USERS"

# Demo accounts, seeded ONLY under the explicit development flag.
DEFAULT_DEV_ACCOUNTS = (
    ("admin", "AirOneAdmin!2026", Role.ADMIN),
    ("viewer", "viewer123", Role.VIEWER),
)

# A real bcrypt hash of a random string; used to equalise timing for unknown
# usernames. It never matches any submitted password in practice.
_DUMMY_HASH = hash_password("airone-dummy-timing-equaliser-Z9!x", rounds=12)


def default_users_allowed() -> bool:
    return os.environ.get(ENV_ALLOW_DEFAULT_USERS, "").lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class UserRecord:
    username: str
    role: str
    must_change_password: bool
    disabled: bool
    is_default_account: bool
    created_at: str
    updated_at: str
    last_login_at: Optional[str]

    def to_public_dict(self) -> Dict[str, object]:
        return {
            "username": self.username,
            "role": self.role,
            "must_change_password": self.must_change_password,
            "disabled": self.disabled,
            "is_default_account": self.is_default_account,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


@dataclass(frozen=True)
class AuthResult:
    state: str  # OK | BAD_CREDENTIALS | DISABLED
    user: Optional[UserRecord] = None

    @property
    def ok(self) -> bool:
        return self.state == "OK"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserStore:
    """CRUD + authentication over the ``users`` table."""

    def __init__(self, db: DatabaseManager, bcrypt_rounds: int = 12) -> None:
        self.db = db
        self.bcrypt_rounds = int(bcrypt_rounds)

    # -- queries -----------------------------------------------------------
    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def get(self, username: str) -> Optional[UserRecord]:
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        return self._row_to_record(row) if row else None

    def list_all(self) -> List[UserRecord]:
        rows = self.db.query("SELECT * FROM users ORDER BY username")
        return [self._row_to_record(r) for r in rows]

    # -- mutations ---------------------------------------------------------
    def create(
        self,
        username: str,
        password: str,
        role: "Role | str",
        *,
        must_change_password: bool = False,
        is_default_account: bool = False,
        enforce_policy: bool = True,
    ) -> UserRecord:
        username = self._normalise_username(username)
        role_name = role.name if isinstance(role, Role) else Role.from_name(str(role)).name
        if self.get(username) is not None:
            raise ValueError(f"User '{username}' already exists")
        if enforce_policy:
            validate_password(password, username)
        now = _now()
        self.db.execute_with_retry(
            """INSERT INTO users (username, password_hash, role, must_change_password,
                                  disabled, is_default_account, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                username, hash_password(password, rounds=self.bcrypt_rounds),
                role_name, int(must_change_password), int(is_default_account),
                now, now,
            ),
        )
        record = self.get(username)
        assert record is not None
        return record

    def set_password(
        self, username: str, new_password: str, *, must_change_password: bool = False
    ) -> UserRecord:
        """Change a password (policy enforced).

        Clears ``is_default_account``; ``must_change_password`` is cleared
        unless the caller (an administrative reset) asks to keep it set.
        """

        record = self.get(username)
        if record is None:
            raise KeyError(f"Unknown user '{username}'")
        validate_password(new_password, username)
        self.db.execute_with_retry(
            """UPDATE users SET password_hash = ?, must_change_password = ?,
                                is_default_account = 0, updated_at = ?
               WHERE username = ?""",
            (hash_password(new_password, rounds=self.bcrypt_rounds),
             int(must_change_password), _now(), username),
        )
        updated = self.get(username)
        assert updated is not None
        return updated

    def set_disabled(self, username: str, disabled: bool) -> None:
        if self.get(username) is None:
            raise KeyError(f"Unknown user '{username}'")
        self.db.execute_with_retry(
            "UPDATE users SET disabled = ?, updated_at = ? WHERE username = ?",
            (int(disabled), _now(), username),
        )

    def delete(self, username: str) -> None:
        self.db.execute_with_retry("DELETE FROM users WHERE username = ?", (username,))

    # -- authentication ----------------------------------------------------
    def authenticate(self, username: str, password: str) -> AuthResult:
        """Verify credentials with timing-equalised behaviour for unknown users."""

        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None:
            # Burn the same bcrypt cost as a real verification.
            verify_password(password, _DUMMY_HASH)
            return AuthResult("BAD_CREDENTIALS")
        if not verify_password(password, row["password_hash"]):
            return AuthResult("BAD_CREDENTIALS")
        record = self._row_to_record(row)
        if record.disabled:
            return AuthResult("DISABLED", record)
        self.db.execute_with_retry(
            "UPDATE users SET last_login_at = ? WHERE username = ?", (_now(), username)
        )
        return AuthResult("OK", record)

    # -- bootstrap ---------------------------------------------------------
    def seed_default_dev_accounts_if_allowed(self) -> List[str]:
        """Seed demo accounts ONLY if the explicit dev flag is set and the store
        is empty. Returns the usernames seeded (possibly empty)."""

        if self.count() > 0:
            return []
        if not default_users_allowed():
            logger.error(
                "No user accounts exist and %s is not set. The API will refuse "
                "every login until an administrator is created: "
                "AIRONE_ADMIN_PASSWORD='<strong>' python3 launcher.py --create-admin",
                ENV_ALLOW_DEFAULT_USERS,
            )
            return []
        seeded = []
        for username, password, role in DEFAULT_DEV_ACCOUNTS:
            self.create(
                username, password, role,
                must_change_password=True, is_default_account=True,
                enforce_policy=False,  # these are the known-weak demo defaults
            )
            seeded.append(username)
        logger.warning(
            "=" * 70 + "\nDEFAULT DEVELOPMENT ACCOUNTS SEEDED (%s=1): %s\n"
            "These accounts have well-known passwords and are flagged "
            "must_change_password. They cannot use the API until their password "
            "is changed. NEVER enable this flag in production.\n" + "=" * 70,
            ENV_ALLOW_DEFAULT_USERS, ", ".join(seeded),
        )
        return seeded

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _normalise_username(username: str) -> str:
        username = (username or "").strip()
        if not (1 <= len(username) <= 64):
            raise ValueError("username must be 1-64 characters")
        if not all(c.isalnum() or c in "._-" for c in username):
            raise ValueError("username may only contain letters, digits, '.', '_' or '-'")
        return username

    @staticmethod
    def _row_to_record(row) -> UserRecord:
        return UserRecord(
            username=row["username"],
            role=row["role"],
            must_change_password=bool(row["must_change_password"]),
            disabled=bool(row["disabled"]),
            is_default_account=bool(row["is_default_account"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_login_at=row["last_login_at"],
        )
