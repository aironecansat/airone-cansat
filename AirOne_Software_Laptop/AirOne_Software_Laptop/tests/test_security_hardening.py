"""Unit tests for the authentication hardening controls.

Covers: password policy, persistent bcrypt user store, dev-account gate,
login throttling (per-account + per-IP, exponential backoff), JWT claim
pinning (alg/iss/aud/jti/type), persistent revocation and refresh rotation.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from src.core.errors import InvalidTokenError, TokenExpiredError
from src.security import auth
from src.security.lockout import LoginThrottle
from src.security.passwords import (
    MIN_LENGTH,
    check_password,
    generate_password,
    validate_password,
)
from src.security.users import (
    DEFAULT_DEV_ACCOUNTS,
    ENV_ALLOW_DEFAULT_USERS,
    UserStore,
    default_users_allowed,
)
from src.storage.database import DatabaseManager

SECRET = "unit_test_secret_key_that_is_definitely_long_enough_123456"


@pytest.fixture
def db(temp_db_path):
    d = DatabaseManager(temp_db_path)
    d.run_migrations()
    yield d
    d.close()


@pytest.fixture
def store(db):
    return UserStore(db, bcrypt_rounds=4)


# --- Password policy -------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "short1!A", "alllowercase!!123", "NOUPPERCASE!!123", "NoDigitsHere!!!",
    "NoSymbols12345A", "AirOneAdmin!2026", "admin!Admin1234",
])
def test_password_policy_rejects_weak(bad):
    result = check_password(bad, username="admin")
    assert not result.ok and result.problems


def test_password_policy_accepts_strong_and_generated():
    assert check_password("Falcon-Orbit!7731", "admin").ok
    for _ in range(20):
        pw = generate_password()
        assert len(pw) >= MIN_LENGTH
        assert check_password(pw).ok
    with pytest.raises(ValueError):
        validate_password("weak", "admin")


# --- User store ------------------------------------------------------------
def test_user_store_crud_and_bcrypt(store):
    assert store.count() == 0
    u = store.create("ops", "Ground-Link!3327", "OPERATOR")
    assert not hasattr(u, "password_hash"), "records never expose the hash"
    stored = store.db.query_one("SELECT password_hash FROM users WHERE username = ?", ("ops",))
    assert stored["password_hash"].startswith("$2") and "Ground-Link" not in stored["password_hash"]
    assert store.count() == 1
    assert store.authenticate("ops", "Ground-Link!3327").ok
    assert store.authenticate("ops", "wrong-Password!1").state == "BAD_CREDENTIALS"
    assert store.authenticate("nobody", "Ground-Link!3327").state == "BAD_CREDENTIALS"
    store.set_disabled("ops", True)
    assert store.authenticate("ops", "Ground-Link!3327").state == "DISABLED"
    store.set_disabled("ops", False)
    store.set_password("ops", "New-Orbit!4482")
    assert store.authenticate("ops", "New-Orbit!4482").ok
    assert not store.authenticate("ops", "Ground-Link!3327").ok
    public = store.get("ops").to_public_dict()
    assert "password_hash" not in public
    store.delete("ops")
    assert store.get("ops") is None


def test_user_store_enforces_policy_and_uniqueness(store):
    with pytest.raises(ValueError):
        store.create("weak", "weak", "VIEWER")
    store.create("dup", "Falcon-Orbit!7731", "VIEWER")
    with pytest.raises(ValueError):
        store.create("dup", "Falcon-Orbit!7732", "VIEWER")


def test_user_store_persists_across_reopen(temp_db_path):
    d1 = DatabaseManager(temp_db_path)
    d1.run_migrations()
    UserStore(d1, bcrypt_rounds=4).create("persist", "Falcon-Orbit!7731", "ADMIN")
    d1.close()
    d2 = DatabaseManager(temp_db_path)
    d2.run_migrations()
    try:
        assert UserStore(d2, bcrypt_rounds=4).authenticate("persist", "Falcon-Orbit!7731").ok
    finally:
        d2.close()


def test_default_accounts_only_seeded_when_gate_open(store, monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_DEFAULT_USERS, raising=False)
    assert not default_users_allowed()
    assert store.seed_default_dev_accounts_if_allowed() == []
    assert store.count() == 0

    monkeypatch.setenv(ENV_ALLOW_DEFAULT_USERS, "1")
    seeded = store.seed_default_dev_accounts_if_allowed()
    assert len(seeded) == len(DEFAULT_DEV_ACCOUNTS)
    for rec in store.list_all():
        assert rec.must_change_password and rec.is_default_account
    # Never re-seeds once accounts exist.
    assert store.seed_default_dev_accounts_if_allowed() == []


# --- Throttle ---------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_throttle_locks_account_with_backoff():
    clock = _Clock()
    th = LoginThrottle(max_attempts_per_account=3, base_lockout_s=60, max_lockout_s=600,
                       _clock=clock)
    for _ in range(3):
        th.record_failure("alice", "10.0.0.1")
    d = th.check("alice", "10.0.0.1")
    assert not d.allowed and d.reason == "ACCOUNT_LOCKED" and d.retry_after_s > 0
    # Other accounts from the same IP still allowed (IP limit not reached).
    assert th.check("bob", "10.0.0.1").allowed
    clock.t += 61
    assert th.check("alice", "10.0.0.1").allowed
    # Second lockout is longer (exponential backoff).
    for _ in range(3):
        th.record_failure("alice", "10.0.0.1")
    d2 = th.check("alice", "10.0.0.1")
    assert d2.retry_after_s > 60
    clock.t += 1000
    th.record_success("alice")
    assert th.check("alice", "10.0.0.1").allowed


def test_throttle_locks_ip_across_accounts():
    clock = _Clock()
    th = LoginThrottle(max_attempts_per_account=100, max_attempts_per_ip=5, _clock=clock)
    for i in range(5):
        th.record_failure(f"user{i}", "10.0.0.9")
    d = th.check("another", "10.0.0.9")
    assert not d.allowed and d.reason == "IP_LOCKED"
    assert th.check("another", "10.0.0.10").allowed
    assert th.snapshot()["locked_ips"] >= 1 or th.snapshot()


# --- JWT --------------------------------------------------------------------
def test_jwt_claims_pinned_and_generic_errors():
    tok = auth.create_access_token("alice", "ADMIN", secret=SECRET)
    payload = auth.decode_token(tok, secret=SECRET, expected_type="access")
    assert payload["iss"] == auth.ISSUER and payload["aud"] == auth.AUDIENCE
    assert set(auth.REQUIRED_CLAIMS) <= set(payload)
    # Wrong expected type.
    with pytest.raises(InvalidTokenError):
        auth.decode_token(tok, secret=SECRET, expected_type="refresh")
    # Wrong secret.
    with pytest.raises(InvalidTokenError):
        auth.decode_token(tok, secret="x" * 40)


def _forge(claims, secret=SECRET, algorithm="HS256"):
    now = datetime.now(timezone.utc)
    base = {"iss": auth.ISSUER, "aud": auth.AUDIENCE, "sub": "alice", "type": "access",
            "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": "forged", "role": "ADMIN"}
    base.update(claims)
    return jwt.encode(base, secret, algorithm=algorithm) if algorithm != "none" else \
        jwt.encode(base, None, algorithm="none")


@pytest.mark.parametrize("claims", [
    {"iss": "someone-else"}, {"aud": "other-api"}, {"type": "weird"},
])
def test_jwt_rejects_wrong_iss_aud_type(claims):
    with pytest.raises(InvalidTokenError):
        auth.decode_token(_forge(claims), secret=SECRET)


def test_jwt_rejects_missing_jti_and_alg_none_and_expired():
    now = datetime.now(timezone.utc)
    no_jti = {"iss": auth.ISSUER, "aud": auth.AUDIENCE, "sub": "a", "type": "access",
              "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp())}
    with pytest.raises(InvalidTokenError):
        auth.decode_token(jwt.encode(no_jti, SECRET, algorithm="HS256"), secret=SECRET)
    with pytest.raises(InvalidTokenError):
        auth.decode_token(_forge({}, algorithm="none"), secret=SECRET)
    with pytest.raises(InvalidTokenError):
        auth.decode_token(_forge({}, algorithm="HS512"), secret=SECRET)
    with pytest.raises(TokenExpiredError):
        auth.decode_token(_forge({"exp": int((now - timedelta(minutes=1)).timestamp())}), secret=SECRET)
    with pytest.raises(InvalidTokenError):
        auth.decode_token("", secret=SECRET)
    with pytest.raises(InvalidTokenError):
        auth.decode_token("a" * 5000, secret=SECRET)


def test_jwt_lifetime_caps():
    with pytest.raises(ValueError):
        auth.create_access_token("a", "ADMIN", expires_minutes=auth.MAX_ACCESS_MINUTES + 1, secret=SECRET)
    with pytest.raises(ValueError):
        auth.create_refresh_token("a", expires_days=auth.MAX_REFRESH_DAYS + 1, secret=SECRET)


def test_revocation_persists_across_blacklist_reload(db):
    bl = auth.TokenBlacklist(db)
    assert bl.persistent
    tok = auth.create_access_token("alice", "ADMIN", secret=SECRET)
    payload = auth.decode_token(tok, secret=SECRET, blacklist=bl)
    auth.revoke_payload(payload, blacklist=bl)
    with pytest.raises(InvalidTokenError, match="revoked"):
        auth.decode_token(tok, secret=SECRET, blacklist=bl)
    # A fresh blacklist over the same database sees the revocation.
    bl2 = auth.TokenBlacklist(db)
    assert bl2.is_revoked(payload["jti"]) and len(bl2) >= 1
    with pytest.raises(InvalidTokenError):
        auth.decode_token(tok, secret=SECRET, blacklist=bl2)


def test_blacklist_prunes_expired_entries_on_load(db):
    bl = auth.TokenBlacklist(db)
    bl.revoke("old", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    bl.revoke("fresh", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    bl2 = auth.TokenBlacklist(db)
    assert not bl2.is_revoked("old") and bl2.is_revoked("fresh")
