"""Flask application factory for the AirOne REST API.

Security posture (see ``docs/security_model.md``):

* **No shipped credentials.** ``build_services`` never creates accounts unless
  ``AIRONE_ALLOW_DEFAULT_USERS=1`` is set explicitly (development only); the
  first administrator is otherwise created with ``launcher.py --create-admin``.
* Persistent :class:`~src.security.users.UserStore` (bcrypt), persistent JWT
  revocation list, per-account/per-IP login throttling.
* Request body size limit, strict schemas, security headers, CORS restricted to
  configured origins (wildcard refused), generic error responses, debug never
  enabled from configuration.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from flask import Flask, g, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from ..core.mission.state_machine import MissionStateMachine
from ..security.auth import TokenBlacklist, set_default_blacklist
from ..security.lockout import LoginThrottle
from ..security.rate_limit import ENDPOINT_LIMITS, create_limiter
from ..security.users import UserStore
from ..storage.database import DatabaseManager
from ..storage.repositories import (
    AnalysisRepository,
    EventRepository,
    MLModelRepository,
    MissionRepository,
    TelemetryRepository,
)
from .middleware import envelope, install_request_context

logger = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES = 256 * 1024
DEFAULT_CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
ENV_ALLOW_CORS_WILDCARD = "AIRONE_ALLOW_CORS_WILDCARD"


@dataclass
class ServiceContainer:
    """Holds shared services injected into the API and workers."""

    db: DatabaseManager
    telemetry_repo: TelemetryRepository
    event_repo: EventRepository
    mission_repo: MissionRepository
    ml_repo: MLModelRepository
    analysis_repo: AnalysisRepository
    mission_machine: MissionStateMachine
    config: Dict[str, Any] = field(default_factory=dict)
    user_store: Optional[UserStore] = None
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    blacklist: Optional[TokenBlacklist] = None
    metrics_provider: Optional[Any] = None
    log_path: str = "logs/airone.jsonl"
    # Live reference to the scientific worker (set by the orchestrator at
    # startup). ``None`` when the analysis tier is not running.
    scientific_worker: Optional[Any] = None
    # Live reference to the ML analysis worker (set by the orchestrator at
    # startup). ``None`` when the ML tier is not running. ML is advisory-only.
    ml_worker: Optional[Any] = None
    # Live reference to the serial/LoRa telemetry transport and a callable
    # returning its explicit link status string (set by the orchestrator).
    serial_transport: Optional[Any] = None
    telemetry_link_status: Optional[Any] = None

    def authenticate(self, username: str, password: str):
        """Delegate to the persistent user store.

        Returns an :class:`~src.security.users.AuthResult` (state ``OK``,
        ``BAD_CREDENTIALS`` or ``DISABLED``).
        """

        from ..security.users import AuthResult

        if self.user_store is None:
            return AuthResult("BAD_CREDENTIALS")
        return self.user_store.authenticate(username, password)


def _security_section(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = config or {}
    sec = cfg.get("security", {})
    return sec if isinstance(sec, dict) else {}


def build_services(
    db_path: str = "data/airone.db",
    config: Optional[Dict[str, Any]] = None,
) -> ServiceContainer:
    """Create the service container backed by ``db_path``.

    Users are **not** seeded here unless ``AIRONE_ALLOW_DEFAULT_USERS`` is
    truthy *and* the user table is empty (development convenience, loudly
    logged, accounts flagged must-change-password).
    """

    db = DatabaseManager(db_path)
    sec = _security_section(config)
    user_store = UserStore(db, bcrypt_rounds=int(sec.get("bcrypt_rounds", 12)))
    throttle = LoginThrottle(
        max_attempts_per_account=int(sec.get("max_login_attempts", 5)),
        max_attempts_per_ip=int(sec.get("max_login_attempts_per_ip", 20)),
        base_lockout_s=float(sec.get("lockout_seconds", 300)),
    )
    blacklist = TokenBlacklist(db)
    set_default_blacklist(blacklist)
    services = ServiceContainer(
        db=db,
        telemetry_repo=TelemetryRepository(db),
        event_repo=EventRepository(db),
        mission_repo=MissionRepository(db),
        ml_repo=MLModelRepository(db),
        analysis_repo=AnalysisRepository(db),
        mission_machine=MissionStateMachine(),
        config=config or {},
        user_store=user_store,
        throttle=throttle,
        blacklist=blacklist,
    )
    user_store.seed_default_dev_accounts_if_allowed()
    if user_store.count() == 0:
        logger.warning(
            "User store is EMPTY: every login will fail until an admin is created "
            "(python3 launcher.py --create-admin)."
        )
    return services


def _resolve_cors_origins(config: Dict[str, Any]) -> List[str]:
    """Read CORS origins from ``api.cors_origins`` (or flat ``cors_origins``).

    A wildcard is refused unless ``AIRONE_ALLOW_CORS_WILDCARD=1`` is set; it is
    replaced by the safe default and an error is logged.
    """

    api_cfg = config.get("api", {}) if isinstance(config.get("api"), dict) else {}
    origins = api_cfg.get("cors_origins", config.get("cors_origins", DEFAULT_CORS_ORIGINS))
    if isinstance(origins, str):
        origins = [origins]
    origins = [str(o) for o in (origins or [])]
    if any(o.strip() == "*" for o in origins):
        if os.environ.get(ENV_ALLOW_CORS_WILDCARD, "").lower() in ("1", "true", "yes"):
            logger.warning("CORS wildcard origin enabled by %s — development only", ENV_ALLOW_CORS_WILDCARD)
        else:
            logger.error(
                "CORS origin '*' refused (set %s=1 to allow in development); "
                "falling back to %s", ENV_ALLOW_CORS_WILDCARD, DEFAULT_CORS_ORIGINS,
            )
            origins = list(DEFAULT_CORS_ORIGINS)
    return origins or list(DEFAULT_CORS_ORIGINS)


def create_app(services: ServiceContainer, config: Optional[Dict[str, Any]] = None) -> Flask:
    app = Flask("airone")
    config = config or {}
    app.config["SERVICES"] = services
    app.config["JSON_SORT_KEYS"] = False
    # Debug mode is never derived from user configuration.
    app.config["DEBUG"] = False
    app.debug = False
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["TRAP_HTTP_EXCEPTIONS"] = False

    api_cfg = config.get("api", {}) if isinstance(config.get("api"), dict) else {}
    max_body = int(api_cfg.get("max_body_bytes", config.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)))
    app.config["MAX_CONTENT_LENGTH"] = max(1024, max_body)

    cors_origins = _resolve_cors_origins(config)
    app.config["CORS_ORIGINS"] = cors_origins
    CORS(
        app, origins=cors_origins, supports_credentials=False,
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        max_age=600,
    )

    install_request_context(app)
    limiter = create_limiter(app)
    app.config["LIMITER"] = limiter

    # Register blueprints.
    from .routes.auth_routes import bp as auth_bp
    from .routes.telemetry_routes import bp as telemetry_bp
    from .routes.mission_routes import bp as mission_bp
    from .routes.ml_routes import bp as ml_bp
    from .routes.config_routes import bp as config_bp
    from .routes.system_routes import bp as system_bp
    from .routes.analysis_routes import bp as analysis_bp
    from .routes.user_routes import bp as users_bp

    for bp in (auth_bp, telemetry_bp, mission_bp, ml_bp, config_bp, system_bp, analysis_bp, users_bp):
        app.register_blueprint(bp)

    # Apply the explicit per-endpoint limits. Defaults come from
    # ENDPOINT_LIMITS; ``api.rate_limits`` in the config may override them.
    if limiter is not None:
        limits = dict(ENDPOINT_LIMITS)
        api_cfg = config.get("api", {}) if isinstance(config.get("api"), dict) else {}
        overrides = api_cfg.get("rate_limits", {}) or {}
        for key, value in overrides.items():
            if key in limits and isinstance(value, str) and value.strip():
                limits[key] = value.strip()
            else:
                logger.warning("Ignoring unknown/invalid rate limit override %r=%r", key, value)
        endpoint_map = {
            "auth.login": limits["auth_login"],
            "auth.refresh": limits["auth_refresh"],
            "auth.change_password": limits["auth_login"],
            "telemetry.export": limits["telemetry_export"],
            "ml.train": limits["ml_train"],
            "config.update_config": limits["config"],
        }
        for endpoint, limit in endpoint_map.items():
            view = app.view_functions.get(endpoint)
            if view is not None:
                app.view_functions[endpoint] = limiter.limit(limit)(view)

    @app.errorhandler(400)
    def _bad(_e):  # noqa: ANN202
        return envelope(False, error="Bad request", status=400)

    @app.errorhandler(404)
    def _nf(_e):  # noqa: ANN202
        return envelope(False, error="Not found", status=404)

    @app.errorhandler(405)
    def _mna(_e):  # noqa: ANN202
        return envelope(False, error="Method not allowed", status=405)

    @app.errorhandler(413)
    def _too_large(_e):  # noqa: ANN202
        return envelope(False, error="Request body too large", status=413)

    @app.errorhandler(429)
    def _rate(_e):  # noqa: ANN202
        return envelope(False, error="Rate limit exceeded", status=429)

    @app.errorhandler(HTTPException)
    def _http(e):  # noqa: ANN202
        # Any other werkzeug HTTP error: standard envelope, generic text.
        return envelope(False, error=e.name, status=e.code or 500)

    @app.errorhandler(Exception)
    def _err(e):  # noqa: ANN202
        # Never leak stack traces, paths or exception text to the client.
        logger.exception("Unhandled API error on %s %s", request.method, request.path)
        return envelope(False, error="Internal server error", status=500)

    logger.info("Flask app created with %d URL rules", len(list(app.url_map.iter_rules())))
    return app
