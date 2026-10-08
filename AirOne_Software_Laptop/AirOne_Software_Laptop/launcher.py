#!/usr/bin/env python3
"""AirOne one-click launcher.

Authored by Team AirOne.

Performs fail-fast environment validation, then starts the orchestrator and all
workers in the correct order. Handles SIGINT/SIGTERM for graceful shutdown.

Usage:
    AIRONE_JWT_SECRET=... python launcher.py [--config config/default_config.yaml] [--gui]

Account bootstrap (no accounts ship with the software):
    AIRONE_JWT_SECRET=... AIRONE_ADMIN_PASSWORD=... python launcher.py --create-admin
    AIRONE_JWT_SECRET=... python launcher.py --create-admin --generate-password
    python launcher.py --create-user NAME --role OPERATOR
    python launcher.py --reset-password NAME
    python launcher.py --verify-audit [logs/audit.jsonl]
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
from typing import Any, Dict, List
import json
import secrets

# Ensure package imports resolve when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from src.system_logging import setup_logging  # noqa: E402

REQUIRED_PACKAGES = [
    "numpy", "scipy", "pandas", "jwt", "bcrypt", "flask",
    "flask_limiter", "marshmallow", "sqlite3",
]


class EnvironmentError_(Exception):
    pass


def _check_python() -> str:
    if sys.version_info < (3, 9):
        raise EnvironmentError_(
            f"Python >= 3.9 required, found {sys.version_info.major}.{sys.version_info.minor}"
        )
    return f"Python {sys.version_info.major}.{sys.version_info.minor} OK"


def _check_packages() -> List[str]:
    import importlib

    ok, missing = [], []
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            ok.append(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise EnvironmentError_(
            f"Missing required packages: {', '.join(missing)}. "
            f"Install with: pip install -r requirements.txt"
        )
    return ok


def _check_jwt_secret() -> str:
    """Auto-generate a JWT secret if one is not set — never fatal."""
    if os.environ.get("AIRONE_JWT_SECRET"):
        return "JWT secret already set in environment"
    # Auto-generate and persist to data/.runtime_secrets.json
    secrets_file = os.path.join(_HERE, "data", ".runtime_secrets.json")
    os.makedirs(os.path.join(_HERE, "data"), exist_ok=True)
    try:
        if os.path.exists(secrets_file):
            d = json.loads(open(secrets_file).read())
            existing = d.get("jwt_secret", "")
            if len(existing) >= 32:
                os.environ["AIRONE_JWT_SECRET"] = existing
                return "JWT secret loaded from data/.runtime_secrets.json"
    except Exception:
        pass
    secret = secrets.token_hex(32)
    try:
        open(secrets_file, "w").write(json.dumps({"jwt_secret": secret}, indent=2))
    except Exception:
        pass
    os.environ["AIRONE_JWT_SECRET"] = secret
    return "JWT secret auto-generated and saved to data/.runtime_secrets.json (WARNING)"


def _check_directories(dirs: List[str]) -> str:
    for d in dirs:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, ".write_test")
        try:
            with open(test, "w") as fh:
                fh.write("ok")
            os.remove(test)
        except OSError as exc:
            raise EnvironmentError_(f"Directory {d} is not writable: {exc}")
    return f"Directories writable: {', '.join(dirs)}"


def _check_port(host: str, port: int) -> str:
    if not (0 < int(port) < 65536):
        raise EnvironmentError_(f"API port {port} out of range")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host if host not in ("0.0.0.0", "::") else "", port))
        except OSError as exc:
            raise EnvironmentError_(f"API port {port} unavailable: {exc}")
    return f"API port {port} available on {host}"


def _check_api_host(host: str) -> str:
    """Report the API bind address honestly (plain HTTP on all interfaces)."""

    if host in ("0.0.0.0", "::", ""):
        return (
            f"API host {host or '0.0.0.0'} binds ALL interfaces — plain HTTP exposed; "
            "put a TLS reverse proxy in front or bind to 127.0.0.1 (WARNING)"
        )
    return f"API host {host} (loopback/explicit)"


def _check_accounts(config: Dict[str, Any]) -> str:
    """Warn when no user account exists and the dev-account gate is closed.

    Never creates anything: bootstrap is an explicit operator action
    (``--create-admin``).
    """

    from src.security.users import default_users_allowed
    from src.storage.database import DatabaseManager
    from src.security.users import UserStore

    db_path = config.get("storage", {}).get("db_path", "data/airone.db")
    if not os.path.exists(db_path):
        if default_users_allowed():
            return "No database yet; DEV demo accounts WILL be seeded (AIRONE_ALLOW_DEFAULT_USERS=1)"
        return ("No database yet and no accounts: run `launcher.py --create-admin` "
                "before the first login (WARNING)")
    db = DatabaseManager(db_path)
    try:
        n = UserStore(db).count()
    finally:
        db.close()
    if n == 0 and not default_users_allowed():
        return ("No user accounts exist: run `launcher.py --create-admin` "
                "(nobody can log in) (WARNING)")
    if default_users_allowed():
        return f"{n} account(s); DEV demo accounts allowed (AIRONE_ALLOW_DEFAULT_USERS=1) — do not use in production (WARNING)"
    return f"{n} user account(s) present"


def _check_link_key(config: Dict[str, Any]) -> str:
    from src.telemetry import protocol

    raw = os.environ.get("AIRONE_LINK_KEY")
    require = bool(config.get("telemetry", {}).get("require_authenticated_frames", False))
    if raw:
        key = protocol.parse_link_key(raw)
        if key is None:
            raise EnvironmentError_(
                "AIRONE_LINK_KEY must be a hex string of at least "
                f"{protocol.MIN_LINK_KEY_BYTES} bytes"
            )
        return f"Telemetry link authentication {'REQUIRED' if require else 'OPTIONAL'} ({len(key)}-byte key)"
    if require:
        raise EnvironmentError_(
            "telemetry.require_authenticated_frames=true but AIRONE_LINK_KEY is not set"
        )
    return "Telemetry link authentication NOT_CONFIGURED (frames accepted unauthenticated)"


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        import json

        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)


def _resolve_host(config: Dict[str, Any]) -> str:
    from src.workers.orchestrator import resolve_api_host

    return resolve_api_host(config.get("api", {}) or {})


def validate_environment(config: Dict[str, Any], logger) -> None:
    api = config.get("api", {}) or {}
    host = _resolve_host(config)
    checks = [
        _check_python,
        _check_packages,
        _check_jwt_secret,
        lambda: _check_directories([
            config.get("storage", {}).get("data_dir", "data"),
            config.get("logging", {}).get("log_dir", "logs"),
        ]),
        lambda: _check_api_host(host),
        lambda: _check_port(host, int(api.get("port", 5000))),
        lambda: _check_link_key(config),
        lambda: _check_accounts(config),
    ]
    logger.info("Running environment validation...")
    for check in checks:
        result = check()
        text = result if isinstance(result, str) else "package check passed"
        if text.endswith("(WARNING)"):
            logger.warning("  [WARN] %s", text[: -len("(WARNING)")].strip())
        else:
            logger.info("  [OK] %s", text)


# ---------------------------------------------------------------------------
# Account management commands (explicit bootstrap; nothing is shipped)
# ---------------------------------------------------------------------------

def _open_user_store(config: Dict[str, Any]):
    from src.security.users import UserStore
    from src.storage.database import DatabaseManager

    db_path = config.get("storage", {}).get("db_path", "data/airone.db")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    db = DatabaseManager(db_path)
    rounds = int((config.get("security", {}) or {}).get("bcrypt_rounds", 12))
    return db, UserStore(db, bcrypt_rounds=rounds)


def _obtain_password(args, logger, generate: bool) -> str:
    """Password precedence: --generate-password > AIRONE_ADMIN_PASSWORD env.

    The password is never accepted on the command line (it would leak via the
    process list and shell history). A generated password is printed ONCE to
    stdout and must be stored by the operator.
    """

    from src.security.passwords import generate_password

    if generate:
        pw = generate_password()
        print(f"GENERATED PASSWORD (shown once, store it now): {pw}")
        return pw
    pw = os.environ.get("AIRONE_ADMIN_PASSWORD", "")
    if not pw:
        raise EnvironmentError_(
            "Set AIRONE_ADMIN_PASSWORD in the environment or pass --generate-password"
        )
    return pw


def run_account_command(args, config: Dict[str, Any], logger) -> int:
    from src.security.audit import AuditEvent, get_audit_logger
    from src.security.passwords import validate_password
    from src.security.rbac import ROLE_NAMES

    db, store = _open_user_store(config)
    audit = get_audit_logger(log_dir=(config.get("logging", {}) or {}).get("log_dir", "logs"))
    try:
        if args.create_admin or args.create_user:
            username = args.create_user or args.admin_username
            role = args.role.upper() if args.create_user else "ADMIN"
            if role not in ROLE_NAMES:
                logger.error("Unknown role %s (valid: %s)", role, ", ".join(ROLE_NAMES))
                return 2
            if store.get(username) is not None:
                logger.error("User %r already exists (use --reset-password)", username)
                return 2
            password = _obtain_password(args, logger, args.generate_password)
            validate_password(password, username)
            store.create(
                username, password, role,
                must_change_password=bool(args.generate_password or args.must_change),
            )
            audit.log(AuditEvent.USER_CREATE, user_id="launcher",
                      details={"username": username, "role": role, "via": "cli"})
            logger.info("Created %s account %r", role, username)
            return 0
        if args.reset_password:
            username = args.reset_password
            if store.get(username) is None:
                logger.error("User %r does not exist", username)
                return 2
            password = _obtain_password(args, logger, args.generate_password)
            validate_password(password, username)
            store.set_password(username, password,
                               must_change_password=bool(args.generate_password or args.must_change))
            audit.log(AuditEvent.PASSWORD_CHANGE, user_id="launcher",
                      details={"username": username, "via": "cli"})
            logger.info("Password reset for %r", username)
            return 0
        if args.list_users:
            users = store.list_all()
            if not users:
                print("No user accounts (run --create-admin).")
            for u in users:
                d = u.to_public_dict()
                print(f"{d['username']:20} {d['role']:10} disabled={d['disabled']} "
                      f"must_change={d['must_change_password']} default={d['is_default_account']}")
            return 0
    except (ValueError, EnvironmentError_) as exc:
        logger.error("Rejected: %s", exc)
        return 2
    finally:
        db.close()
    return 0


def run_verify_audit(path: str, logger) -> int:
    from src.security.audit import verify_audit_file

    result = verify_audit_file(path)
    for k, v in result.to_dict().items():
        print(f"{k}: {v}")
    if result.ok:
        logger.info("Audit chain OK (%d entries)", result.entries)
        return 0
    logger.error("Audit chain BROKEN at line %s: %s", result.first_bad_line, result.reason)
    return 2


def _start_simulation_feeder(orchestrator, args, stop_flag, logger) -> threading.Thread:
    """Start a background thread that injects simulated binary packets.

    The simulator produces the same on-the-wire frames as the radio link, so
    simulated data traverses the identical parser -> pipeline -> workers path.
    """

    from src.simulation import FlightProfile, MissionSimulator, SimulatedPacketSource

    # If a link key is configured the simulated frames carry the same
    # authentication tag as real firmware, so a REQUIRED policy still admits
    # them (they stay tagged SIMULATED in the payload).
    link_key = getattr(orchestrator, "link_key", None)

    def _feed() -> None:
        loop = 0
        while not stop_flag.is_set():
            simulator = MissionSimulator(
                profile=FlightProfile(apogee_m=args.sim_apogee),
                sample_rate_hz=args.sim_rate,
                seed=args.sim_seed + loop,
            )
            source = SimulatedPacketSource(simulator, link_key=link_key)
            interval = 1.0 / max(0.1, args.sim_rate)
            n = 0
            for packet in source.packets():
                if stop_flag.is_set():
                    return
                orchestrator.inject(packet)
                n += 1
                time.sleep(interval)
            logger.info("Simulation pass %d complete (%d packets)", loop, n)
            if not args.sim_loop:
                logger.info("Simulation finished (use --sim-loop to replay).")
                return
            loop += 1

    thread = threading.Thread(target=_feed, name="SimulationFeeder", daemon=True)
    thread.start()
    logger.info(
        "Simulation feeder started (apogee=%.0f m, rate=%.1f Hz, seed=%d, loop=%s)",
        args.sim_apogee, args.sim_rate, args.sim_seed, args.sim_loop,
    )
    return thread


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AirOne launcher")
    parser.add_argument("--config", default=os.path.join(_HERE, "config", "default_config.yaml"))
    parser.add_argument("--gui", action="store_true", help="Start PyQt5 GUI if available")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--simulate", action="store_true",
        help="Feed a physics-based simulated mission through the real pipeline",
    )
    parser.add_argument("--sim-seed", type=int, default=12345, help="Simulation RNG seed")
    parser.add_argument("--sim-apogee", type=float, default=1000.0, help="Simulated apogee (m)")
    parser.add_argument("--sim-rate", type=float, default=2.0, help="Simulated sample rate (Hz)")
    parser.add_argument("--sim-loop", action="store_true", help="Continuously replay the simulated mission")
    # -- Real telemetry link (LoRa E22 over USB serial) overrides --------
    parser.add_argument(
        "--serial-port", default=None,
        help="Explicit serial device for the CanSat radio (e.g. /dev/ttyUSB0, "
             "COM5). Default: auto-discover known USB-serial bridges.",
    )
    parser.add_argument(
        "--baud", type=int, default=None,
        help="Serial baud rate for the E22 link (default from config: 115200).",
    )
    parser.add_argument(
        "--force-baud", action="store_true",
        help="Open the port directly at --baud with no MAGIC negotiation "
             "(recommended for E22 transparent mode at a fixed baud).",
    )
    parser.add_argument(
        "--no-serial", action="store_true",
        help="Do not start the live telemetry link (explicitly disable it).",
    )
    # -- Account bootstrap / maintenance (no accounts ship with the software) --
    acct = parser.add_argument_group("account management")
    acct.add_argument("--create-admin", action="store_true",
                      help="Create the first ADMIN account (password from "
                           "AIRONE_ADMIN_PASSWORD or --generate-password) and exit.")
    acct.add_argument("--admin-username", default="admin", help="Username for --create-admin")
    acct.add_argument("--create-user", metavar="NAME", default=None,
                      help="Create an account with --role and exit")
    acct.add_argument("--role", default="VIEWER",
                      help="Role for --create-user (ADMIN, ENGINEER, SCIENTIST, OPERATOR, VIEWER)")
    acct.add_argument("--reset-password", metavar="NAME", default=None,
                      help="Reset an account password (AIRONE_ADMIN_PASSWORD or --generate-password)")
    acct.add_argument("--generate-password", action="store_true",
                      help="Generate a strong random password, print it once, and force a change at first login")
    acct.add_argument("--must-change", action="store_true",
                      help="Force a password change at first login")
    acct.add_argument("--list-users", action="store_true", help="List accounts and exit")
    acct.add_argument("--verify-audit", nargs="?", const="__default__", default=None, metavar="PATH",
                      help="Verify the audit log hash chain and exit (default: <log_dir>/audit.jsonl)")
    args = parser.parse_args(argv)

    config = load_config(args.config)

    # Apply serial/telemetry CLI overrides onto the loaded config so the
    # orchestrator picks them up when it opens the live link.
    tel_cfg = config.setdefault("telemetry", {})
    if args.no_serial:
        tel_cfg["enabled"] = False
    # Precedence: --serial-port > AIRONE_SERIAL_PORT env > config value.
    if args.serial_port is not None:
        tel_cfg["serial_port"] = args.serial_port
    elif os.environ.get("AIRONE_SERIAL_PORT"):
        tel_cfg["serial_port"] = os.environ["AIRONE_SERIAL_PORT"]
    if args.baud is not None:
        tel_cfg["baud_rate"] = args.baud
    if args.force_baud:
        tel_cfg["force_baud"] = True
    log_cfg = config.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_dir=log_cfg.get("log_dir", "logs"),
        json_output=log_cfg.get("json_output", True),
    )
    import logging

    logger = logging.getLogger("airone.launcher")

    if args.verify_audit is not None:
        path = args.verify_audit
        if path == "__default__":
            path = os.path.join(log_cfg.get("log_dir", "logs"), "audit.jsonl")
        return run_verify_audit(path, logger)

    if args.create_admin or args.create_user or args.reset_password or args.list_users:
        return run_account_command(args, config, logger)

    try:
        validate_environment(config, logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("Environment validation FAILED: %s", exc)
        return 2

    if args.validate_only:
        logger.info("Validation-only mode: all checks passed.")
        return 0

    # Build services and orchestrator.
    from src.api.app import build_services
    from src.workers.orchestrator import Orchestrator

    db_path = config.get("storage", {}).get("db_path", "data/airone.db")
    services = build_services(db_path, config)
    orchestrator = Orchestrator(services, config)

    stop_flag = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Received signal %s; initiating shutdown", signum)
        stop_flag.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    orchestrator.start()

    # Report the explicit live telemetry link state (set during start()).
    link_status = orchestrator.telemetry_link_status
    logger.info("Telemetry link: %s", link_status)

    sim_thread = None
    if args.simulate:
        if link_status.startswith("LIVE"):
            logger.warning(
                "--simulate requested while the LIVE radio link is up. Simulated "
                "packets are tagged SIMULATED and share the pipeline for testing; "
                "real telemetry remains authoritative. Omit --simulate for a "
                "pure live mission."
            )
        else:
            logger.info(
                "Simulation mode is for ISOLATED testing/training only "
                "(no live link active)."
            )
        sim_thread = _start_simulation_feeder(orchestrator, args, stop_flag, logger)

    if args.gui:
        from src.gui import gui_available, run_gui

        ok, reason = gui_available()
        if not ok:
            logger.warning("--gui requested but GUI unavailable (%s); continuing headless",
                           reason)
        else:
            api_cfg = config.get("api", {})
            port = int(api_cfg.get("port", 5000))
            logger.info("Launching GUI (blocks main thread until closed)")
            # The GUI owns the main thread; when it closes we shut down cleanly.
            try:
                run_gui(config=config, base_url=f"http://127.0.0.1:{port}")
            finally:
                stop_flag.set()
                orchestrator.shutdown()
                logger.info("AirOne stopped cleanly (GUI closed).")
            return 0

    logger.info("AirOne is running. Press Ctrl+C to stop.")
    try:
        while not stop_flag.is_set():
            time.sleep(0.5)
    finally:
        orchestrator.shutdown()
        logger.info("AirOne stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
