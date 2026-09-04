"""Password policy enforcement for AirOne V7.1 accounts.

Every account password — bootstrap admin, CLI-created operators, or a changed
password — must satisfy :func:`validate_password`. The policy is deliberately
explicit (no hidden heuristics) so operators can reason about it:

* at least ``MIN_LENGTH`` characters (12) and at most ``MAX_LENGTH`` (256);
* at least one lowercase letter, one uppercase letter, one digit and one
  symbol;
* not on the deny-list of well-known / previously shipped defaults;
* must not contain the username (case-insensitive).
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from typing import List

MIN_LENGTH = 12
MAX_LENGTH = 256

# Passwords that must never be accepted. Includes the historic demo defaults
# that earlier AirOne releases shipped with.
DENY_LIST = frozenset({
    "aironeadmin!2026",
    "viewer123",
    "password",
    "password123",
    "changeme",
    "changethis",
    "admin",
    "administrator",
    "letmein",
    "qwerty123",
    "123456789012",
    "airone",
    "cansat",
})


@dataclass
class PasswordCheck:
    ok: bool
    problems: List[str] = field(default_factory=list)


def check_password(password: str, username: str = "") -> PasswordCheck:
    """Return a :class:`PasswordCheck` describing every policy violation."""

    problems: List[str] = []
    if not isinstance(password, str):
        return PasswordCheck(False, ["password must be a string"])
    if len(password) < MIN_LENGTH:
        problems.append(f"must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        problems.append(f"must be at most {MAX_LENGTH} characters")
    if not any(c.islower() for c in password):
        problems.append("must contain a lowercase letter")
    if not any(c.isupper() for c in password):
        problems.append("must contain an uppercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    if not any((not c.isalnum()) and not c.isspace() for c in password):
        problems.append("must contain a symbol")
    if password.lower() in DENY_LIST:
        problems.append("is a well-known default password")
    if username and len(username) >= 3 and username.lower() in password.lower():
        problems.append("must not contain the username")
    return PasswordCheck(not problems, problems)


def validate_password(password: str, username: str = "") -> None:
    """Raise ``ValueError`` listing every violation if the password is weak."""

    result = check_password(password, username)
    if not result.ok:
        raise ValueError("Password policy violation: " + "; ".join(result.problems))


def generate_password(length: int = 20) -> str:
    """Generate a random password that satisfies the policy (for bootstrap)."""

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(max(length, MIN_LENGTH)))
        if check_password(candidate).ok:
            return candidate
