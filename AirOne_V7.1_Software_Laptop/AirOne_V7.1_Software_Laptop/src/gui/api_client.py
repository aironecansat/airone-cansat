"""REST client for the AirOne ground-station GUI.

The GUI is a *pure client* of the Flask REST API. It runs in-process for
convenience (started by the launcher) but talks to the backend only over
HTTP. This guarantees the design rule that **a GUI failure can never corrupt
or stop the backend** — the worst a broken GUI can do is stop polling.

Scientific-honesty rules enforced here:

* The client NEVER fabricates a value. Every call returns an :class:`ApiResult`
  whose ``ok`` flag and ``state`` string explicitly describe what happened.
* Transport failures, auth failures and backend "degraded" states are all
  surfaced verbatim; they are never silently turned into empty/zero data.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

try:
    import requests
except Exception:  # pragma: no cover - requests is a hard dep of the API anyway
    requests = None  # type: ignore


class ConnectionState(str, Enum):
    """Explicit, user-visible connection states. Never guessed."""

    UNKNOWN = "UNKNOWN"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"            # reachable but backend reports a problem
    UNAUTHENTICATED = "UNAUTHENTICATED"
    DISCONNECTED = "DISCONNECTED"    # transport failure
    UNAVAILABLE = "UNAVAILABLE"      # requests library missing


@dataclass
class ApiResult:
    """Result of one API call. Carries an explicit state, never fake data."""

    ok: bool
    data: Any = None
    error: str = ""
    status: int = 0
    state: str = ""

    @property
    def unavailable(self) -> bool:
        return not self.ok


@dataclass
class GroundStationClient:
    """Thread-safe REST client with explicit connection-state tracking."""

    base_url: str = "http://127.0.0.1:5000"
    username: str = ""
    # No default credential: an empty password means "never attempt a login".
    password: str = ""
    timeout: float = 4.0

    _access_token: Optional[str] = field(default=None, init=False, repr=False)
    _refresh_token: Optional[str] = field(default=None, init=False, repr=False)
    _role: Optional[str] = field(default=None, init=False)
    _state: ConnectionState = field(default=ConnectionState.UNKNOWN, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def role(self) -> Optional[str]:
        return self._role

    @property
    def authenticated(self) -> bool:
        return self._access_token is not None

    def _set_state(self, state: ConnectionState) -> None:
        with self._lock:
            self._state = state

    # -- low level ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    def _request(
        self, method: str, path: str, *, json: Any = None, auth: bool = True,
        _retry: bool = True,
    ) -> ApiResult:
        if requests is None:
            self._set_state(ConnectionState.UNAVAILABLE)
            return ApiResult(False, error="python 'requests' not installed",
                             state="UNAVAILABLE")
        if auth and not self._access_token:
            login = self.login()
            if not login.ok:
                return login
        try:
            resp = requests.request(
                method, self._url(path), json=json,
                headers=self._headers(), timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - all transport errors are explicit
            self._set_state(ConnectionState.DISCONNECTED)
            return ApiResult(False, error=f"transport error: {exc}",
                             state="DISCONNECTED")

        # Attempt to decode the standard envelope.
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}

        if resp.status_code == 401 and auth and _retry:
            # Token likely expired; re-auth once.
            self._access_token = None
            relog = self.login()
            if relog.ok:
                return self._request(method, path, json=json, auth=auth, _retry=False)
            self._set_state(ConnectionState.UNAUTHENTICATED)
            return ApiResult(False, error="unauthenticated", status=401,
                             state="UNAUTHENTICATED")

        success = bool(body.get("success", resp.ok))
        data = body.get("data")
        error = body.get("error") or ("" if success else f"HTTP {resp.status_code}")
        # Backend-declared degradation state (NOT_CONFIGURED/UNAVAILABLE/...).
        declared_state = ""
        if isinstance(data, dict):
            declared_state = str(data.get("state", ""))

        if success:
            self._set_state(ConnectionState.CONNECTED)
        elif resp.status_code >= 500 or declared_state in {"NOT_CONFIGURED", "DEGRADED"}:
            self._set_state(ConnectionState.DEGRADED)
        else:
            self._set_state(ConnectionState.CONNECTED)  # reachable, request-level error

        return ApiResult(success, data=data, error=error,
                         status=resp.status_code, state=declared_state)

    # -- auth --------------------------------------------------------------
    def login(self) -> ApiResult:
        if requests is None:
            self._set_state(ConnectionState.UNAVAILABLE)
            return ApiResult(False, error="python 'requests' not installed",
                             state="UNAVAILABLE")
        if not self.username or not self.password:
            # Explicit state; no guessing of well-known credentials.
            self._set_state(ConnectionState.UNAUTHENTICATED)
            return ApiResult(False, error="no credentials configured (AIRONE_GUI_USER/AIRONE_GUI_PASSWORD)",
                             state="UNAUTHENTICATED")
        try:
            resp = requests.post(
                self._url("/api/v1/auth/login"),
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_state(ConnectionState.DISCONNECTED)
            return ApiResult(False, error=f"transport error: {exc}",
                             state="DISCONNECTED")
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not resp.ok or not body.get("success"):
            self._set_state(ConnectionState.UNAUTHENTICATED)
            declared = (body.get("data") or {}).get("state") if isinstance(body.get("data"), dict) else None
            if declared == "PASSWORD_CHANGE_REQUIRED":
                return ApiResult(False, error="password change required before login "
                                 "(POST /api/v1/auth/change-password)",
                                 status=resp.status_code, state="PASSWORD_CHANGE_REQUIRED")
            if declared in {"ACCOUNT_LOCKED", "IP_LOCKED"}:
                return ApiResult(False, error=f"login throttled: {declared}",
                                 status=resp.status_code, state=declared)
            return ApiResult(False, error=body.get("error", "login failed"),
                             status=resp.status_code, state="UNAUTHENTICATED")
        data = body.get("data", {})
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        self._role = data.get("role")
        self._set_state(ConnectionState.CONNECTED)
        return ApiResult(True, data=data, status=resp.status_code, state="CONNECTED")

    # -- typed endpoint helpers -------------------------------------------
    def status(self) -> ApiResult:
        return self._request("GET", "/api/v1/status", auth=False)

    def health(self) -> ApiResult:
        return self._request("GET", "/api/v1/health")

    def latest_telemetry(self) -> ApiResult:
        return self._request("GET", "/api/v1/telemetry/latest")

    def history(self, sensor_id: str, start: str, end: str, limit: int = 5000) -> ApiResult:
        q = f"?sensor_id={sensor_id}&start={start}&end={end}&limit={limit}"
        return self._request("GET", f"/api/v1/telemetry/history{q}")

    def mission_primary(self) -> ApiResult:
        return self._request("GET", "/api/v1/mission/primary")

    def mission_secondary(self) -> ApiResult:
        return self._request("GET", "/api/v1/mission/secondary")

    def mission(self) -> ApiResult:
        return self._request("GET", "/api/v1/mission")

    def analysis_catalogue(self) -> ApiResult:
        return self._request("GET", "/api/v1/analysis")

    def analysis_results(self) -> ApiResult:
        return self._request("GET", "/api/v1/analysis/results")

    def run_analysis(self, name: str, mission_id: str = "default", limit: int = 500) -> ApiResult:
        return self._request(
            "POST", f"/api/v1/analysis/{name}/run",
            json={"mission_id": mission_id, "limit": limit},
        )

    def narrative(self, mission_id: str = "default") -> ApiResult:
        return self._request("GET", f"/api/v1/narrative?mission_id={mission_id}")

    def fusion(self, mission_id: str = "default") -> ApiResult:
        return self._request("GET", f"/api/v1/fusion?mission_id={mission_id}")

    def ml_models(self) -> ApiResult:
        return self._request("GET", "/api/v1/ml/models")

    def ml_detectors(self) -> ApiResult:
        return self._request("GET", "/api/v1/ml/detectors")

    def ml_anomalies(self) -> ApiResult:
        return self._request("GET", "/api/v1/ml/anomalies")

    def ml_drift(self) -> ApiResult:
        return self._request("GET", "/api/v1/ml/drift")

    def ml_train(self, model_type: str = "ensemble", name: str = "",
                 parameters: dict = None) -> ApiResult:
        return self._request(
            "POST", "/api/v1/ml/train",
            json={"model_type": model_type, "name": name or model_type,
                  "parameters": parameters or {}},
        )

    def events(self) -> ApiResult:
        return self._request("GET", "/api/v1/events")

    def system_metrics(self) -> ApiResult:
        return self._request("GET", "/api/v1/system/metrics")

    def get_config(self) -> ApiResult:
        return self._request("GET", "/api/v1/config")
