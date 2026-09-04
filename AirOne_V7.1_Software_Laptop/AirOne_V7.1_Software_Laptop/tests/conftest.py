"""Shared pytest fixtures and environment setup.

A strong JWT secret is set BEFORE any ``src.security`` import so the import-time
enforcement in ``security/config.py`` passes for the test suite.
"""
import os
import sys

# Set a valid secret before security modules are imported.
os.environ.setdefault(
    "AIRONE_JWT_SECRET", "unit_test_secret_key_that_is_definitely_long_enough_123456"
)

# Make the package importable as ``src.*``.
_AIRONE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AIRONE_ROOT not in sys.path:
    sys.path.insert(0, _AIRONE_ROOT)

import pytest  # noqa: E402


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_airone.db")


# --- Test accounts ---------------------------------------------------------
# The application ships NO credentials. Tests create their own accounts with
# policy-compliant passwords through the persistent UserStore.
TEST_USERS = {
    "admin": ("Falcon-Orbit!7731-Test", "ADMIN"),
    "engineer": ("Rocket-Nozzle!4482-Test", "ENGINEER"),
    "scientist": ("Baro-Gradient!9915-Test", "SCIENTIST"),
    "operator": ("Ground-Link!3327-Test", "OPERATOR"),
    "viewer": ("Read-Only!6604-Test", "VIEWER"),
}


def seed_test_users(services):
    """Create the standard test accounts in ``services.user_store``."""

    from src.security.rbac import Role

    for username, (password, role) in TEST_USERS.items():
        if services.user_store.get(username) is None:
            services.user_store.create(username, password, Role[role])
    return services


@pytest.fixture
def seeded_services(temp_db_path):
    from src.api.app import build_services

    services = build_services(db_path=temp_db_path, config={"mission_id": "default", "security": {"bcrypt_rounds": 4}})
    return seed_test_users(services)
