"""API failure-mode and contract tests.

Verifies the honest-degradation contract at the HTTP boundary:
  * consistent response envelope on both success and error;
  * 401 without a token, 403 for insufficient role;
  * 404 for unknown analysis / model;
  * 503 (NOT_CONFIGURED) when an advisory tier is not wired in;
  * 400/422 for malformed or un-satisfiable training requests.
"""
from __future__ import annotations

import pytest

from src.api.app import build_services, create_app
from src.security.rbac import Role
from tests.conftest import TEST_USERS, seed_test_users


@pytest.fixture
def client(temp_db_path):
    services = build_services(db_path=temp_db_path, config={"mission_id": "default", "security": {"bcrypt_rounds": 4}})
    services.db.run_migrations()
    seed_test_users(services)
    app = create_app(services, config={})
    app.config.update(TESTING=True)
    with app.test_client() as c:
        c._services = services  # stash for tests that need direct access
        yield c


def _login(client, username, password):
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})
    return resp


def _token(client, username="admin", password=None):
    password = password or TEST_USERS[username][0]
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# envelope shape
# --------------------------------------------------------------------------
def test_success_envelope_shape(client):
    resp = client.get("/api/v1/status")
    body = resp.get_json()
    assert resp.status_code == 200
    for key in ("success", "data", "error", "request_id", "timestamp"):
        assert key in body
    assert body["success"] is True
    assert body["error"] is None


def test_login_success_and_failure(client):
    ok = _login(client, "admin", TEST_USERS["admin"][0])
    assert ok.status_code == 200
    assert ok.get_json()["data"]["access_token"]

    bad = _login(client, "admin", "wrong-password")
    assert bad.status_code == 401
    body = bad.get_json()
    assert body["success"] is False
    assert body["error"]


# --------------------------------------------------------------------------
# auth / RBAC gating
# --------------------------------------------------------------------------
def test_protected_endpoint_requires_token(client):
    resp = client.get("/api/v1/ml/models")
    assert resp.status_code == 401
    assert resp.get_json()["success"] is False


def test_role_gating_forbids_viewer_on_scientist_route(client):
    token = _token(client, "viewer")
    resp = client.get("/api/v1/ml/models", headers=_auth(token))
    assert resp.status_code == 403
    assert resp.get_json()["success"] is False


def test_admin_can_list_models(client):
    token = _token(client)
    resp = client.get("/api/v1/ml/models", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert "models" in resp.get_json()["data"]


# --------------------------------------------------------------------------
# 404 / not-found
# --------------------------------------------------------------------------
def test_unknown_model_returns_404(client):
    token = _token(client)
    resp = client.get("/api/v1/ml/models/99999", headers=_auth(token))
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


def test_unknown_analysis_returns_error(client):
    token = _token(client)
    resp = client.get("/api/v1/analysis/no_such_analysis", headers=_auth(token))
    assert resp.status_code in (404, 400)
    assert resp.get_json()["success"] is False


# --------------------------------------------------------------------------
# 503 NOT_CONFIGURED when advisory tier absent
# --------------------------------------------------------------------------
def test_anomalies_503_when_ml_worker_absent(client):
    # No orchestrator ran, so services.ml_worker is None.
    token = _token(client)
    resp = client.get("/api/v1/ml/anomalies", headers=_auth(token))
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["success"] is False
    assert body["data"]["state"] == "NOT_CONFIGURED"


def test_detectors_availability_reported(client):
    token = _token(client)
    resp = client.get("/api/v1/ml/detectors", headers=_auth(token))
    assert resp.status_code == 200
    detectors = resp.get_json()["data"]["detectors"]
    assert "robust_zscore" in detectors
    assert detectors["robust_zscore"]["available"] is True


# --------------------------------------------------------------------------
# training request validation
# --------------------------------------------------------------------------
def test_train_unknown_model_type_400(client):
    token = _token(client)
    resp = client.post("/api/v1/ml/train", headers=_auth(token),
                      json={"model_type": "totally_fake"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_train_without_data_422(client):
    token = _token(client)
    # Valid model type but no stored telemetry -> honest 422, not a fake model.
    resp = client.post("/api/v1/ml/train", headers=_auth(token),
                      json={"model_type": "robust_zscore"})
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["success"] is False
    assert body["data"]["state"] in ("UNAVAILABLE", None) or "state" in body["data"]
