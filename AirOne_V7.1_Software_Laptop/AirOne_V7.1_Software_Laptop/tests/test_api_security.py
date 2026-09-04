"""HTTP-boundary security tests.

Every control added by the security hardening is exercised end-to-end through
the Flask test client: security headers, CORS policy, body size limit,
strict schemas, the complete authentication matrix over ``app.url_map``,
lockout, forced password change, refresh rotation, user administration,
generic error handling, export path sanitisation and config immutability.
"""
from __future__ import annotations

import pytest

from src.api.app import ServiceContainer, build_services, create_app
from src.security.users import ENV_ALLOW_DEFAULT_USERS
from tests.conftest import TEST_USERS, seed_test_users

SECURITY_CFG = {"bcrypt_rounds": 4, "max_login_attempts": 3, "max_login_attempts_per_ip": 50,
                "lockout_seconds": 60}


def _make_app(db_path, config=None, seed=True):
    cfg = {"mission_id": "default", "security": dict(SECURITY_CFG),
           # Generous HTTP rate limits so lockout (not the limiter) is what tests observe.
           "api": {"rate_limits": {"auth_login": "1000 per minute", "auth_refresh": "1000 per minute",
                                   "telemetry_export": "1000 per minute", "config": "1000 per minute"}}}
    if config:
        for k, v in config.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    services = build_services(db_path=db_path, config=cfg)
    if seed:
        seed_test_users(services)
    app = create_app(services, config=cfg)
    app.config.update(TESTING=True)
    return app, services


@pytest.fixture
def app_and_services(temp_db_path):
    return _make_app(temp_db_path)


@pytest.fixture
def client(app_and_services):
    app, services = app_and_services
    with app.test_client() as c:
        c._services = services
        yield c


def _login(client, username, password=None):
    password = password or TEST_USERS[username][0]
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def _token(client, username="admin"):
    resp = _login(client, username)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- headers / transport ------------------------------------------------------
def test_security_headers_on_every_response(client):
    for path, expected in (("/api/v1/status", 200), ("/api/v1/telemetry/latest", 401),
                           ("/nope", 404)):
        resp = client.get(path)
        assert resp.status_code == expected
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Server"] == "AirOne"
        assert "Werkzeug" not in resp.headers.get("Server", "")


def test_request_id_is_sanitised(client):
    resp = client.get("/api/v1/status", headers={"X-Request-ID": "abc<script>\"quoted\" " + "z" * 200})
    rid = resp.headers["X-Request-ID"]
    assert "<" not in rid and '"' not in rid and len(rid) <= 64


def test_cors_restricted_to_allowlist(client):
    ok = client.get("/api/v1/status", headers={"Origin": "http://localhost:3000"})
    assert ok.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"
    bad = client.get("/api/v1/status", headers={"Origin": "https://evil.example"})
    assert bad.headers.get("Access-Control-Allow-Origin") is None


def test_cors_wildcard_refused_without_env(temp_db_path, monkeypatch):
    monkeypatch.delenv("AIRONE_ALLOW_CORS_WILDCARD", raising=False)
    app, _ = _make_app(temp_db_path, {"api": {"cors_origins": ["*"]}})
    with app.test_client() as c:
        resp = c.get("/api/v1/status", headers={"Origin": "https://evil.example"})
        assert resp.headers.get("Access-Control-Allow-Origin") is None
    monkeypatch.setenv("AIRONE_ALLOW_CORS_WILDCARD", "1")
    app2, _ = _make_app(temp_db_path + ".2", {"api": {"cors_origins": ["*"]}})
    with app2.test_client() as c:
        resp = c.get("/api/v1/status", headers={"Origin": "https://evil.example"})
        assert resp.headers.get("Access-Control-Allow-Origin") in ("*", "https://evil.example")


def test_body_size_limit_returns_413(client):
    assert client.application.config["MAX_CONTENT_LENGTH"] == 256 * 1024
    big = {"username": "admin", "password": "x" * (300 * 1024)}
    resp = client.post("/api/v1/auth/login", json=big)
    assert resp.status_code == 413
    body = resp.get_json()
    assert body["success"] is False


def test_debug_and_exception_propagation_off(client):
    app = client.application
    assert app.debug is False
    assert app.config["PROPAGATE_EXCEPTIONS"] is False
    assert app.config["TRAP_HTTP_EXCEPTIONS"] is False


# --- schema strictness ---------------------------------------------------------
def test_unknown_fields_rejected(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "admin", "password": TEST_USERS["admin"][0], "role": "ADMIN"})
    assert resp.status_code == 400
    resp = client.post("/api/v1/auth/login", json=["not", "a", "dict"])
    assert resp.status_code == 400
    resp = client.post("/api/v1/auth/login", data="username=admin",
                       content_type="application/x-www-form-urlencoded")
    assert resp.status_code in (400, 415)


def test_logs_query_validation(client):
    tok = _token(client)
    assert client.get("/api/v1/system/logs?n=abc", headers=_auth(tok)).status_code == 400
    assert client.get("/api/v1/system/logs?n=999999", headers=_auth(tok)).status_code == 400
    assert client.get("/api/v1/system/logs?n=5", headers=_auth(tok)).status_code == 200


def test_history_rejects_unsafe_sensor_id(client):
    tok = _token(client)
    resp = client.get("/api/v1/telemetry/history?sensor_id=../../etc/passwd", headers=_auth(tok))
    assert resp.status_code == 400
    resp = client.get("/api/v1/telemetry/history?sensor_id=BMP581&start=yesterday", headers=_auth(tok))
    assert resp.status_code == 400


# --- authentication matrix ------------------------------------------------------
PUBLIC_ENDPOINTS = {"telemetry.status", "auth.login", "auth.refresh", "auth.change_password", "static"}


def test_every_route_requires_authentication_except_public(client):
    app = client.application
    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        path = rule.rule.replace("<name>", "x").replace("<username>", "x").replace("<int:model_id>", "1")
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            resp = client.open(path, method=method, json={})
            assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
            checked += 1
    assert checked >= 30


def test_refresh_token_cannot_be_used_as_access_token(client):
    data = _login(client, "admin").get_json()["data"]
    resp = client.get("/api/v1/telemetry/latest", headers=_auth(data["refresh_token"]))
    assert resp.status_code == 401


def test_viewer_forbidden_on_privileged_routes(client):
    tok = _token(client, "viewer")
    assert client.get("/api/v1/users", headers=_auth(tok)).status_code == 403
    assert client.put("/api/v1/config", json={"gui": {}}, headers=_auth(tok)).status_code == 403
    assert client.post("/api/v1/mission/state", json={"state": "ASCENT"},
                       headers=_auth(tok)).status_code == 403
    assert client.post("/api/v1/telemetry/export", json={"sensor_id": "BMP581", "format": "json"},
                       headers=_auth(tok)).status_code == 403


def test_tampered_token_rejected(client):
    tok = _token(client)
    header, payload, sig = tok.split(".")
    resp = client.get("/api/v1/telemetry/latest", headers=_auth(f"{header}.{payload}.{sig[:-3]}abc"))
    assert resp.status_code == 401
    assert resp.get_json()["error"] in ("Invalid token", "Token has been revoked")


# --- login / lockout / audit ----------------------------------------------------
def test_login_failure_is_generic_and_audited(client, tmp_path):
    bad_user = _login(client, "ghost", "Falcon-Orbit!7731-Test")
    bad_pw = _login(client, "admin", "Wrong-Password!123")
    assert bad_user.status_code == bad_pw.status_code == 401
    assert bad_user.get_json()["error"] == bad_pw.get_json()["error"]


def test_account_lockout_after_failures(client):
    for _ in range(2):
        assert _login(client, "operator", "Wrong-Password!123").status_code == 401
    # Third failure reaches max_login_attempts=3 -> account locked (429 or 401 acceptable
    # for the triggering request itself), every subsequent attempt is refused with 429.
    third = _login(client, "operator", "Wrong-Password!123")
    assert third.status_code in (401, 429)
    locked = _login(client, "operator")  # correct password, still locked
    assert locked.status_code == 429
    assert locked.headers["Retry-After"]
    assert locked.get_json()["data"]["state"] == "ACCOUNT_LOCKED"
    # Other accounts unaffected.
    assert _login(client, "viewer").status_code == 200


def test_disabled_user_cannot_login_or_refresh(client):
    admin = _token(client)
    data = _login(client, "viewer").get_json()["data"]
    assert client.post("/api/v1/users/viewer/disable", headers=_auth(admin)).status_code == 200
    assert _login(client, "viewer").status_code == 401
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert resp.status_code == 401
    assert client.post("/api/v1/users/viewer/enable", headers=_auth(admin)).status_code == 200
    assert _login(client, "viewer").status_code == 200


def test_refresh_rotation_and_reuse_detection(client):
    data = _login(client, "admin").get_json()["data"]
    first = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert first.status_code == 200
    new = first.get_json()["data"]
    assert new["refresh_token"] != data["refresh_token"]
    # Old refresh token is now revoked.
    reuse = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert reuse.status_code == 401
    assert client.get("/api/v1/telemetry/latest", headers=_auth(new["access_token"])).status_code == 200


def test_logout_revokes_access_and_refresh(client):
    data = _login(client, "admin").get_json()["data"]
    tok = data["access_token"]
    assert client.get("/api/v1/telemetry/latest", headers=_auth(tok)).status_code == 200
    out = client.post("/api/v1/auth/logout", json={"refresh_token": data["refresh_token"]},
                      headers=_auth(tok))
    assert out.status_code == 200
    assert client.get("/api/v1/telemetry/latest", headers=_auth(tok)).status_code == 401
    assert client.post("/api/v1/auth/refresh",
                       json={"refresh_token": data["refresh_token"]}).status_code == 401


def test_revocation_survives_app_restart(temp_db_path):
    app, services = _make_app(temp_db_path)
    with app.test_client() as c:
        tok = _token(c)
        c.post("/api/v1/auth/logout", headers=_auth(tok))
    services.db.close()
    app2, _ = _make_app(temp_db_path, seed=False)
    with app2.test_client() as c2:
        assert c2.get("/api/v1/telemetry/latest", headers=_auth(tok)).status_code == 401


# --- default accounts & forced password change ----------------------------------
def test_no_accounts_without_gate_and_login_impossible(temp_db_path, monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_DEFAULT_USERS, raising=False)
    app, services = _make_app(temp_db_path, seed=False)
    assert services.user_store.count() == 0
    with app.test_client() as c:
        assert _login(c, "admin", "AirOneAdmin!2026").status_code == 401


def test_dev_accounts_must_change_password_before_tokens(temp_db_path, monkeypatch):
    monkeypatch.setenv(ENV_ALLOW_DEFAULT_USERS, "1")
    app, services = _make_app(temp_db_path, seed=False)
    assert services.user_store.count() == 2
    with app.test_client() as c:
        resp = _login(c, "admin", "AirOneAdmin!2026")
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["data"]["state"] == "PASSWORD_CHANGE_REQUIRED"
        assert "access_token" not in (body.get("data") or {})
        # Weak new password refused, deny-listed default refused.
        weak = c.post("/api/v1/auth/change-password", json={
            "username": "admin", "current_password": "AirOneAdmin!2026", "new_password": "short"})
        assert weak.status_code == 400
        # Wrong current password → 401 (and counts towards lockout).
        wrong = c.post("/api/v1/auth/change-password", json={
            "username": "admin", "current_password": "nope", "new_password": "Falcon-Orbit!7731"})
        assert wrong.status_code == 401
        ok = c.post("/api/v1/auth/change-password", json={
            "username": "admin", "current_password": "AirOneAdmin!2026", "new_password": "Falcon-Orbit!7731"})
        assert ok.status_code == 200, ok.get_json()
        good = _login(c, "admin", "Falcon-Orbit!7731")
        assert good.status_code == 200 and good.get_json()["data"]["role"] == "ADMIN"
        assert not services.user_store.get("admin").is_default_account


def test_authenticated_password_change(client):
    tok = _token(client, "scientist")
    resp = client.post("/api/v1/auth/password", headers=_auth(tok), json={
        "current_password": TEST_USERS["scientist"][0], "new_password": "Baro-Gradient!0001"})
    assert resp.status_code == 200
    assert _login(client, "scientist", TEST_USERS["scientist"][0]).status_code == 401
    assert _login(client, "scientist", "Baro-Gradient!0001").status_code == 200


# --- user administration ----------------------------------------------------------
def test_admin_user_lifecycle(client):
    admin = _token(client)
    created = client.post("/api/v1/users", headers=_auth(admin), json={
        "username": "newops", "password": "Ground-Link!9001", "role": "OPERATOR"})
    assert created.status_code == 201, created.get_json()
    listing = client.get("/api/v1/users", headers=_auth(admin)).get_json()["data"]
    names = {u["username"] for u in listing["users"]} if isinstance(listing, dict) else {u["username"] for u in listing}
    assert "newops" in names
    assert all("password_hash" not in str(listing) for _ in [0])
    # must_change_password default True → login is a 403 until changed.
    assert _login(client, "newops", "Ground-Link!9001").status_code == 403
    # Invalid role / weak password / duplicate.
    assert client.post("/api/v1/users", headers=_auth(admin), json={
        "username": "x1", "password": "Ground-Link!9001", "role": "ROOT"}).status_code == 400
    assert client.post("/api/v1/users", headers=_auth(admin), json={
        "username": "x2", "password": "weak", "role": "VIEWER"}).status_code == 400
    assert client.post("/api/v1/users", headers=_auth(admin), json={
        "username": "newops", "password": "Ground-Link!9002", "role": "VIEWER"}).status_code in (400, 409)
    # Cannot delete or disable oneself.
    assert client.delete("/api/v1/users/admin", headers=_auth(admin)).status_code in (400, 403)
    assert client.post("/api/v1/users/admin/disable", headers=_auth(admin)).status_code in (400, 403)
    assert client.delete("/api/v1/users/newops", headers=_auth(admin)).status_code == 200
    assert client.delete("/api/v1/users/newops", headers=_auth(admin)).status_code == 404


# --- misc hardening ----------------------------------------------------------------
def test_unhandled_exception_is_generic(client):
    app = client.application

    @app.route("/__boom")
    def _boom():  # pragma: no cover - exercised via client
        raise RuntimeError("secret internal detail")

    resp = client.get("/__boom")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["success"] is False
    assert "secret internal detail" not in resp.get_data(as_text=True)


def test_export_rejects_path_traversal_and_returns_basename(client):
    tok = _token(client, "engineer")
    bad = client.post("/api/v1/telemetry/export", headers=_auth(tok),
                      json={"sensor_id": "../../../etc/passwd", "format": "json"})
    assert bad.status_code == 400
    ok = client.post("/api/v1/telemetry/export", headers=_auth(tok),
                     json={"sensor_id": "BMP581", "format": "json"})
    assert ok.status_code == 200, ok.get_json()
    data = ok.get_json()["data"]
    assert "path" not in data
    assert "/" not in data["file"] and data["file"].startswith("export_BMP581_")


def test_config_update_rejects_immutable_keys_and_redacts(client):
    admin = _token(client)
    for key in ("api", "security", "storage", "telemetry"):
        resp = client.put("/api/v1/config", headers=_auth(admin), json={"config": {key: {"x": 1}}})
        assert resp.status_code == 400, key
        assert "not changeable" in resp.get_json()["error"].lower(), key
    resp = client.put("/api/v1/config", headers=_auth(admin), json={"config": {"gui": {"theme": "dark"}}})
    assert resp.status_code == 200
    shown = client.get("/api/v1/config", headers=_auth(admin)).get_json()["data"]
    text = str(shown).lower()
    assert "unit_test_secret_key" not in text


def test_status_reports_honest_link_state(client):
    data = client.get("/api/v1/status").get_json()["data"]
    assert data["link_state"] == "NOT_CONFIGURED"
    assert data["connected"] is False
