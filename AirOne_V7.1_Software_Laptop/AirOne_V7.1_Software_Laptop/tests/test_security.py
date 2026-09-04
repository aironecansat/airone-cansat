"""Tests for authentication, JWT, RBAC, audit chain, and secret enforcement."""
import time

import pytest

from src.core.errors import (
    InsecureConfigError,
    PermissionDeniedError,
    TokenExpiredError,
)
from src.security import auth, config
from src.security.audit import AuditEvent, AuditLogger
from src.security.rbac import Role, check_permission, role_has_permission


def test_password_hash_and_verify():
    hashed = auth.hash_password("s3cr3t-passphrase")
    assert hashed != "s3cr3t-passphrase"
    assert auth.verify_password("s3cr3t-passphrase", hashed) is True
    assert auth.verify_password("wrong", hashed) is False


def test_jwt_create_decode():
    token = auth.create_access_token("user-1", "ENGINEER", expires_minutes=15)
    payload = auth.decode_token(token)
    assert payload["sub"] == "user-1"
    assert payload["role"] == "ENGINEER"
    assert payload["type"] == "access"
    assert "jti" in payload


def test_jwt_expired_raises():
    token = auth.create_access_token("user-2", "VIEWER", expires_minutes=-1)
    with pytest.raises(TokenExpiredError):
        auth.decode_token(token)


def test_default_secret_rejected():
    with pytest.raises(InsecureConfigError):
        config.validate_jwt_secret("ChangeThisSecret")
    with pytest.raises(InsecureConfigError):
        config.validate_jwt_secret("short")


def test_rbac_viewer_cannot_delete():
    assert role_has_permission(Role.VIEWER, "read_telemetry") is True
    assert role_has_permission(Role.VIEWER, "delete_data") is False
    with pytest.raises(PermissionDeniedError):
        check_permission(Role.VIEWER, "delete_data")
    # Engineer can delete.
    assert role_has_permission(Role.ENGINEER, "delete_data") is True


def test_audit_log_chain(tmp_path):
    audit = AuditLogger(log_dir=str(tmp_path))
    e1 = audit.log(AuditEvent.LOGIN_SUCCESS, user_id="a", role="ADMIN")
    e2 = audit.log(AuditEvent.CONFIG_CHANGE, user_id="a", role="ADMIN")
    assert e2["previous_checksum"] == e1["entry_checksum"]
    assert audit.verify_chain() is True
    # Tamper with the first entry and confirm the chain breaks.
    audit._entries_in_memory[0]["details"] = {"tampered": True}
    assert audit.verify_chain() is False


def test_token_revocation():
    token = auth.create_access_token("user-3", "VIEWER")
    payload = auth.decode_token(token)
    auth.revoke_token(payload["jti"])
    with pytest.raises(Exception):
        auth.decode_token(token)
