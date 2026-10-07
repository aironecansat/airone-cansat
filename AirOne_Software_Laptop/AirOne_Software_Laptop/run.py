#!/usr/bin/env python3
"""AirOne CanSat Ground Station — zero-config launcher.

Usage:
    python run.py                       # auto-discover USB serial port
    python run.py --port /dev/ttyUSB0  # explicit port
    python run.py --port COM5          # Windows
    python run.py --baud 115200        # override baud (default: 115200)
    python run.py --no-browser         # don't auto-open browser

Demo/simulation mode is locked until 10 real flights have been logged.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE))

# Live console output even when piped / launched from a shortcut.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

DATA_DIR = Path(os.environ.get("AIRONE_DATA_DIR") or (_HERE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "flights.db"
SECRETS_FILE = DATA_DIR / ".runtime_secrets.json"

# ---------------------------------------------------------------------------
# Runtime secret (auto-generated, never committed)
# ---------------------------------------------------------------------------

def _load_or_create_secret() -> str:
    if SECRETS_FILE.exists():
        try:
            d = json.loads(SECRETS_FILE.read_text())
            if len(d.get("jwt_secret", "")) >= 32:
                return d["jwt_secret"]
        except Exception:
            pass
    sec = secrets.token_hex(32)
    SECRETS_FILE.write_text(json.dumps({"jwt_secret": sec}, indent=2))
    return sec

# Set before any imports that might read it.
if not os.environ.get("AIRONE_JWT_SECRET"):
    os.environ["AIRONE_JWT_SECRET"] = _load_or_create_secret()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS packets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            sequence   INTEGER,
            mission_state TEXT,
            json_text  TEXT,
            rssi       REAL,
            snr        REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flights (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            started   REAL,
            landed    REAL,
            completed INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _count_completed_flights(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM flights WHERE completed=1").fetchone()
    return int(row[0]) if row else 0


def _log_packet(conn: sqlite3.Connection, seq: int, state: str,
                json_text: str, rssi: Optional[float], snr: Optional[float]) -> None:
    conn.execute(
        "INSERT INTO packets (ts, sequence, mission_state, json_text, rssi, snr) VALUES (?,?,?,?,?,?)",
        (time.time(), seq, state, json_text, rssi, snr),
    )
    conn.commit()


def _update_flight_tracking(conn: sqlite3.Connection, state: str) -> None:
    """Detect BOOT→flight start and LANDED→flight complete."""
    if state in ("ASCENT", "DESCENT", "COAST"):
        # Ensure an open flight exists.
        open_row = conn.execute(
            "SELECT id FROM flights WHERE completed=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not open_row:
            conn.execute("INSERT INTO flights (started, completed) VALUES (?,0)", (time.time(),))
            conn.commit()
    elif state == "LANDED":
        open_row = conn.execute(
            "SELECT id FROM flights WHERE completed=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if open_row:
            conn.execute(
                "UPDATE flights SET landed=?, completed=1 WHERE id=?",
                (time.time(), open_row[0]),
            )
            conn.commit()

# ---------------------------------------------------------------------------
# SSE subscriber bus
# ---------------------------------------------------------------------------

class _SSEBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues = [x for x in self._queues if x is not q]

    def publish(self, data: dict) -> None:
        text = "data: " + json.dumps(data) + "\n\n"
        with self._lock:
            dead = []
            for q in self._queues:
                try:
                    q.put_nowait(text)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._queues.remove(q)


_bus = _SSEBus()

# ---------------------------------------------------------------------------
# Shared state (written by serial thread, read by Flask)
# ---------------------------------------------------------------------------

_state = {
    "connected": False,
    "port": None,
    "baud": None,
    "packets_rx": 0,
    "last_packet_ts": None,
    "last_fields": {},
    "log": [],         # last 20 messages
    "db": None,
    "flights_completed": 0,
}
_state_lock = threading.Lock()


def _log_msg(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"[AirOne] {msg}")
    with _state_lock:
        _state["log"].append(line)
        if len(_state["log"]) > 50:
            _state["log"] = _state["log"][-50:]
    _bus.publish({"type": "log", "line": line})

# ---------------------------------------------------------------------------
# Serial / packet thread
# ---------------------------------------------------------------------------

def _serial_thread(args: argparse.Namespace) -> None:
    """Background thread: auto-discovers port, reads bytes, feeds parser."""
    # Import here so Flask can start even if these fail (they won't on a
    # normal install, but keeps the import error local to this thread).
    try:
        from src.communication.serial_transport import SerialTransport
        from src.telemetry.parser import StreamParser
    except ImportError as exc:
        _log_msg(f"Import error: {exc}. Check requirements_run.txt.")
        return

    import serial  # type: ignore

    parser = StreamParser()
    ser: Optional[serial.Serial] = None
    current_port: Optional[str] = None
    prev_mission_state: Optional[str] = None

    def _try_connect(port: str, baud: int) -> Optional[serial.Serial]:
        try:
            s = serial.Serial(port, baud, timeout=0.5)
            _log_msg(f"Serial: connected {port} @ {baud}")
            with _state_lock:
                _state["connected"] = True
                _state["port"] = port
                _state["baud"] = baud
            return s
        except Exception as exc:
            _log_msg(f"Serial: cannot open {port}: {exc}")
            return None

    while True:
        # ---- Find a port ----
        if ser is None:
            with _state_lock:
                _state["connected"] = False

            target_port = args.port
            if target_port is None:
                found = SerialTransport.discover_ports()
                target_port = found[0] if found else None

            if target_port:
                ser = _try_connect(target_port, args.baud)
                if ser:
                    current_port = target_port
                    parser.reset()
            else:
                time.sleep(2)
                continue

        # ---- Read bytes ----
        try:
            chunk = ser.read(4096)
        except Exception as exc:
            _log_msg(f"Serial: read error on {current_port}: {exc}")
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            current_port = None
            time.sleep(1)
            continue

        if not chunk:
            time.sleep(0.05)
            continue

        packets = parser.parse_stream(chunk)
        for pkt in packets:
            if not pkt.crc_valid or pkt.duplicate:
                continue

            # Decode JSON payload
            try:
                fields = json.loads(pkt.payload)
            except Exception:
                continue

            mission_state = ""
            if "mission_state" in fields:
                ms_f = fields["mission_state"]
                mission_state = ms_f.get("value", "") if isinstance(ms_f, dict) else str(ms_f)

            # Log to DB
            db = _state.get("db")
            if db is not None:
                try:
                    _log_packet(db, pkt.sequence, mission_state,
                                pkt.payload.decode("utf-8", errors="replace"),
                                None, None)
                    _update_flight_tracking(db, mission_state)
                    with _state_lock:
                        _state["flights_completed"] = _count_completed_flights(db)
                except Exception as exc:
                    _log_msg(f"DB error: {exc}")

            with _state_lock:
                _state["packets_rx"] += 1
                _state["last_packet_ts"] = time.time()
                _state["last_fields"] = fields
                flights = _state["flights_completed"]

            # Build summary line for console
            alt = ""
            temp = ""
            if "altitude_rel" in fields:
                v = fields["altitude_rel"]
                alt = f"ALT: {v.get('value', v):.1f} m" if isinstance(v, dict) else f"ALT: {v}"
            if "temperature" in fields:
                v = fields["temperature"]
                temp = f"TEMP: {v.get('value', v):.1f}°C" if isinstance(v, dict) else f"TEMP: {v}"
            parts = [x for x in [f"Pkt#{pkt.sequence}", alt, temp,
                                  f"STATE:{mission_state}" if mission_state else ""] if x]
            print(f"[AirOne] {' | '.join(parts)}")

            # Publish to SSE
            _bus.publish({
                "seq": pkt.sequence,
                "ts": time.time(),
                "fields": fields,
                "mission_state": mission_state,
                "packets_rx": _state["packets_rx"],
                "port": current_port,
                "flights_completed": flights,
                "rssi": None,
                "snr": None,
            })

            if mission_state and mission_state != prev_mission_state:
                _log_msg(f"Mission state: {prev_mission_state} → {mission_state}")
                prev_mission_state = mission_state

# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------

def _build_app() -> "Flask":
    from flask import Flask, Response, jsonify, send_from_directory  # type: ignore
    from src.ui.dashboard import ui_bp

    app = Flask(__name__, static_folder=None)
    # Pass shared state to the blueprint via app config (avoids circular imports).
    app.config["AIRONE_BUS"]        = _bus
    app.config["AIRONE_STATE"]      = _state
    app.config["AIRONE_STATE_LOCK"] = _state_lock
    app.register_blueprint(ui_bp)

    @app.route("/status")
    def status():
        with _state_lock:
            s = dict(_state)
        age = (time.time() - s["last_packet_ts"]) if s["last_packet_ts"] else None
        pps = 0.0
        if age is not None and age < 60:
            pps = s["packets_rx"] / max(1.0, time.time() - (_state.get("start_ts") or time.time()))
        return jsonify({
            "connected": s["connected"],
            "port": s["port"],
            "baud": s["baud"],
            "packets_rx": s["packets_rx"],
            "packet_rate_hz": round(pps, 2),
            "last_packet_age_s": round(age, 1) if age is not None else None,
            "flights_completed": s["flights_completed"],
            "sim_unlocked": s["flights_completed"] >= 10,
            "log": s["log"][-20:],
        })

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="AirOne CanSat Ground Station — plug in the USB bridge and go."
    )
    parser.add_argument("--port", default=None,
                        help="Serial port (default: auto-discover CP210x/CH340/FTDI)")
    parser.add_argument("--baud", type=int, default=115200,
                        help="Baud rate (default: 115200)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not auto-open the browser")
    parser.add_argument("--flask-port", type=int, default=5000,
                        help="Dashboard port (default: 5000)")
    parser.add_argument("--demo", action="store_true",
                        help="[LOCKED] Simulation mode — unlocks after 10 real flights")
    args = parser.parse_args()

    # Demo gate
    conn_tmp = _init_db(DB_PATH)
    completed = _count_completed_flights(conn_tmp)
    conn_tmp.close()

    if args.demo:
        if completed < 10:
            print(f"[AirOne] Demo mode is locked until 10 real flights have been logged.")
            print(f"[AirOne] Current: {completed}/10 completed flights in {DB_PATH}")
            return 1
        else:
            print("[AirOne] 10+ flights logged — demo mode unlocked. "
                  "Use --simulate with the full launcher.py instead.")
            return 0

    # Init DB and store in shared state
    db_conn = _init_db(DB_PATH)
    with _state_lock:
        _state["db"] = db_conn
        _state["flights_completed"] = _count_completed_flights(db_conn)
        _state["start_ts"] = time.time()

    print("[AirOne] Starting AirOne CanSat Ground Station")
    print(f"[AirOne] Database: {DB_PATH}")
    print(f"[AirOne] Completed flights logged: {_state['flights_completed']}")
    print(f"[AirOne] Dashboard: http://localhost:{args.flask_port}")
    print("[AirOne] Waiting for USB ground bridge... (Ctrl+C to stop)")

    # Start serial thread
    t = threading.Thread(target=_serial_thread, args=(args,), name="SerialThread", daemon=True)
    t.start()

    # Auto-open browser
    if not args.no_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{args.flask_port}")
        threading.Thread(target=_open, daemon=True).start()

    # Start Flask (blocks)
    app = _build_app()
    try:
        app.run(host="127.0.0.1", port=args.flask_port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[AirOne] Shutdown.")
        try:
            db_conn.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
