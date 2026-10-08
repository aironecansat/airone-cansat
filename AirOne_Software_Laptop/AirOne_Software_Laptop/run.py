#!/usr/bin/env python3
"""AirOne CanSat Ground Station — zero-config launcher.

Usage:
    python run.py                       # auto-discover USB serial port
    python run.py --port /dev/ttyUSB0  # explicit port
    python run.py --port COM5          # Windows
    python run.py --baud 115200        # override baud (default: 115200)
    python run.py --no-browser         # don't auto-open browser
    python run.py --sim                # replay a recorded (completed) flight from the DB

The dashboard shows nothing until a real packet arrives (live serial, or a
recorded packet replayed with --sim). No placeholder values are ever generated.

AI features (/api/chat, /api/digital-twin) use a local Ollama server:
    ollama serve && ollama pull deepseek-r1:8b
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _update_flight_tracking(conn: sqlite3.Connection, state: str) -> Optional[int]:
    """Detect BOOT→flight start and LANDED→flight complete.

    Returns the id of the flight this packet belongs to (open, or just
    completed by this LANDED packet), or None when no flight is in progress.
    """
    if state in ("ASCENT", "DESCENT", "COAST"):
        # Ensure an open flight exists.
        open_row = conn.execute(
            "SELECT id FROM flights WHERE completed=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if open_row:
            return int(open_row[0])
        cur = conn.execute("INSERT INTO flights (started, completed) VALUES (?,0)", (time.time(),))
        conn.commit()
        return int(cur.lastrowid)
    if state == "LANDED":
        open_row = conn.execute(
            "SELECT id FROM flights WHERE completed=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if open_row:
            conn.execute(
                "UPDATE flights SET landed=?, completed=1 WHERE id=?",
                (time.time(), open_row[0]),
            )
            conn.commit()
            return int(open_row[0])
    return None


# Flights store start/landing timestamps; a flight's packets are those whose
# receive time (packets.ts) falls inside [started, landed].

def _list_completed_flights(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT f.id, f.started, f.landed, "
        "  (SELECT COUNT(*) FROM packets p WHERE p.ts BETWEEN f.started AND f.landed) "
        "FROM flights f WHERE f.completed=1 ORDER BY f.id"
    ).fetchall()
    out = []
    for fid, started, landed, count in rows:
        out.append({
            "id": int(fid),
            "start_time": started,
            "packet_count": int(count or 0),
            "duration_s": round(landed - started, 1) if started is not None and landed is not None else None,
        })
    return out


def _load_flight_packets(conn: sqlite3.Connection, flight_id: int) -> Optional[List[Dict[str, Any]]]:
    """Return [{'seq', 'ts', 'fields'}] ordered by receive time, or None if no such completed flight."""
    row = conn.execute(
        "SELECT started, landed FROM flights WHERE id=? AND completed=1", (int(flight_id),)
    ).fetchone()
    if not row:
        return None
    rows = conn.execute(
        "SELECT sequence, ts, json_text FROM packets WHERE ts BETWEEN ? AND ? ORDER BY ts, id",
        (row[0], row[1]),
    ).fetchall()
    packets = []
    for seq, ts, text in rows:
        try:
            fields = json.loads(text)
        except Exception:
            continue
        if isinstance(fields, dict):
            packets.append({"seq": seq, "ts": ts, "fields": fields})
    return packets

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

    def publish(self, event: str, data: Any) -> None:
        """Send a named SSE event (``status`` / ``packet`` / ``meta`` / ``log``)."""
        text = "event: " + event + "\ndata: " + json.dumps(data) + "\n\n"
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

# Public state: exactly what /status returns and what `event: status` carries.
# Starts empty -- nothing is invented before the first real packet.
_state: Dict[str, Any] = {
    "packets_rx": 0,
    "connected": False,
    "port": None,
    "flight_id": None,
    "mode": "normal",
    "sim_flight_id": None,
    "last_packet": None,
}
_state_lock = threading.Lock()

# Internal (not exposed through /status).
_db: Optional[sqlite3.Connection] = None
_db_lock = threading.Lock()           # sqlite connection is shared by serial thread + Flask
_log_lines: "collections.deque[str]" = collections.deque(maxlen=50)
_recent_packets: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=600)  # live, for AI

# Recorded-flight replay (--sim). Packets come from the DB, never generated.
g_sim_flight: Dict[str, Any] = {"active": False, "packets": [], "index": 0}
SIM_INTERVAL_S = 1.0

# Local LLM (Ollama).
OLLAMA_URL = os.environ.get("AIRONE_OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("AIRONE_OLLAMA_MODEL", "deepseek-r1:8b")
OLLAMA_TIMEOUT_S = float(os.environ.get("AIRONE_OLLAMA_TIMEOUT", "180"))
AI_OFFLINE_MSG = "AI offline — run: ollama serve && ollama pull deepseek-r1:8b"
AI_SYSTEM_PROMPT = ("You are AirOne AI, an expert flight telemetry analyst for CanSat competitions. "
                    "Answer questions about the flight data provided. Be concise and technical.")


def _status_snapshot() -> Dict[str, Any]:
    with _state_lock:
        return dict(_state)


def _publish_status() -> None:
    _bus.publish("status", _status_snapshot())


def _log_msg(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"[AirOne] {msg}")
    _log_lines.append(line)
    _bus.publish("log", {"line": line})


def _emit_packet(seq: Optional[int], fields: Dict[str, Any], rssi: Optional[float] = None) -> None:
    """Record a real packet in the shared state and push it to every dashboard."""
    with _state_lock:
        _state["packets_rx"] += 1
        _state["last_packet"] = fields
        count = _state["packets_rx"]
    if rssi is None:
        r = fields.get("rssi")
        if isinstance(r, dict):
            r = r.get("value")
        rssi = r if isinstance(r, (int, float)) else None
    # meta = frame header info (not sensor data); packet = exactly the parsed sensor dict.
    _bus.publish("meta", {"seq": seq, "packets_rx": count, "rssi": rssi})
    _bus.publish("packet", fields)

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
            _publish_status()
            return s
        except Exception as exc:
            _log_msg(f"Serial: cannot open {port}: {exc}")
            return None

    while True:
        # ---- Find a port ----
        if ser is None:
            with _state_lock:
                was_connected = _state["connected"]
                _state["connected"] = False
                _state["port"] = None
            if was_connected:
                _publish_status()

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
            if not isinstance(fields, dict):
                continue

            mission_state = ""
            if "mission_state" in fields:
                ms_f = fields["mission_state"]
                mission_state = ms_f.get("value", "") if isinstance(ms_f, dict) else str(ms_f)

            # Log to DB
            if _db is not None:
                try:
                    with _db_lock:
                        _log_packet(_db, pkt.sequence, mission_state,
                                    pkt.payload.decode("utf-8", errors="replace"),
                                    None, None)
                        fid = _update_flight_tracking(_db, mission_state)
                    with _state_lock:
                        changed = fid is not None and fid != _state["flight_id"]
                        if fid is not None:
                            _state["flight_id"] = fid
                    if changed:
                        _publish_status()
                except Exception as exc:
                    _log_msg(f"DB error: {exc}")

            _recent_packets.append(fields)

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
            _emit_packet(pkt.sequence, fields)

            if mission_state and mission_state != prev_mission_state:
                _log_msg(f"Mission state: {prev_mission_state} → {mission_state}")
                prev_mission_state = mission_state

# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------

def _build_app() -> "Flask":
    from flask import Flask, jsonify, request  # type: ignore
    from src.ui.dashboard import ui_bp

    app = Flask(__name__, static_folder=None)
    # Pass shared state to the blueprint via app config (avoids circular imports).
    app.config["AIRONE_BUS"]        = _bus
    app.config["AIRONE_STATE"]      = _state
    app.config["AIRONE_STATE_LOCK"] = _state_lock
    app.register_blueprint(ui_bp)

    @app.route("/status")
    @app.route("/api/status")
    def status():
        return jsonify(_status_snapshot())

    @app.route("/api/flights")
    def api_flights():
        if _db is None:
            return jsonify({"flights": []})
        with _db_lock:
            flights = _list_completed_flights(_db)
        return jsonify({"flights": flights})

    @app.route("/api/sim/load", methods=["POST"])
    def api_sim_load():
        body = request.get_json(silent=True) or {}
        if _status_snapshot()["mode"] != "sim":
            return jsonify({"error": "Replay is only available when started with: python run.py --sim"}), 409
        try:
            flight_id = int(body.get("flight_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "flight_id (integer) required"}), 400
        ok, err = _sim_load(flight_id)
        if not ok:
            return jsonify({"error": err}), 404
        return jsonify({"ok": True, "flight_id": flight_id,
                        "packet_count": len(g_sim_flight["packets"])})

    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        body = request.get_json(silent=True) or {}
        message = str(body.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message required"}), 400
        context = body.get("context")
        user_text = message
        if context:
            user_text = ("Latest telemetry packet (JSON):\n" + json.dumps(context, default=str)[:12000]
                         + "\n\nQuestion: " + message)
        try:
            raw = _ollama_chat([{"role": "system", "content": AI_SYSTEM_PROMPT},
                                {"role": "user", "content": user_text}])
        except _AIOffline:
            return jsonify({"error": AI_OFFLINE_MSG}), 503
        thinking, reply = _split_thinking(raw)
        return jsonify({"reply": reply, "thinking": thinking})

    @app.route("/api/digital-twin", methods=["POST"])
    def api_digital_twin():
        body = request.get_json(silent=True) or {}
        summary = body.get("summary")
        if not summary:
            summary, err = _flight_summary_for(body.get("flight_id"))
            if summary is None:
                return jsonify({"error": err}), 404
        prompt = (
            "Act as a digital twin of this CanSat. From the flight summary below, identify the main "
            "mission risks. Reply with ONLY a JSON object, no prose, in exactly this shape:\n"
            '{"risks":[{"id":"R1","severity":"HIGH|MEDIUM|LOW","title":"...","description":"...",'
            '"mitigation":"..."}],"overall_risk":"HIGH|MEDIUM|LOW","ai_reasoning":"..."}\n'
            "Base every risk on the data; do not invent measurements.\n\nFlight summary:\n"
            + (summary if isinstance(summary, str) else json.dumps(summary, default=str))[:16000]
        )
        try:
            raw = _ollama_chat([{"role": "system", "content": AI_SYSTEM_PROMPT},
                                {"role": "user", "content": prompt}], json_mode=True)
        except _AIOffline:
            return jsonify({"error": AI_OFFLINE_MSG}), 503
        return jsonify(_parse_twin(raw))

    return app


# ---------------------------------------------------------------------------
# Recorded-flight replay
# ---------------------------------------------------------------------------

def _sim_load(flight_id: int):
    """Load a completed flight's packets into g_sim_flight. Returns (ok, error)."""
    if _db is None:
        return False, "database not available"
    with _db_lock:
        packets = _load_flight_packets(_db, flight_id)
    if packets is None:
        return False, f"no completed flight with id {flight_id}"
    if not packets:
        return False, f"flight {flight_id} has no recorded packets"
    with _state_lock:
        g_sim_flight["active"] = True
        g_sim_flight["packets"] = packets
        g_sim_flight["index"] = 0
        _state["sim_flight_id"] = flight_id
        _state["flight_id"] = flight_id
        _state["packets_rx"] = 0
        _state["last_packet"] = None
    _publish_status()
    _log_msg(f"SIM: replaying recorded flight #{flight_id} ({len(packets)} packets @ 1 Hz)")
    return True, None


def _sim_thread() -> None:
    """Serve the loaded flight's recorded packets at ~1 Hz over the SSE bus."""
    while True:
        item = None
        done_id = None
        with _state_lock:
            if g_sim_flight["active"]:
                i = g_sim_flight["index"]
                if i < len(g_sim_flight["packets"]):
                    item = g_sim_flight["packets"][i]
                    g_sim_flight["index"] = i + 1
                else:
                    g_sim_flight["active"] = False
                    done_id = _state["sim_flight_id"]
        if item is not None:
            _emit_packet(item["seq"], item["fields"])
        elif done_id is not None:
            _log_msg(f"SIM: flight #{done_id} replay complete")
        time.sleep(SIM_INTERVAL_S)


# ---------------------------------------------------------------------------
# Local AI (Ollama)
# ---------------------------------------------------------------------------

class _AIOffline(Exception):
    pass


def _ollama_chat(messages: List[Dict[str, str]], json_mode: bool = False) -> str:
    """POST to Ollama /api/chat (stream=false). Raises _AIOffline if unreachable/failed."""
    payload: Dict[str, Any] = {"model": OLLAMA_MODEL, "messages": messages, "stream": False}
    if json_mode:
        payload["format"] = "json"
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log_msg(f"AI: Ollama request failed: {exc}")
        raise _AIOffline(str(exc))
    msg = data.get("message") or {}
    content = str(msg.get("content") or "")
    # Newer Ollama versions may return reasoning separately.
    if msg.get("thinking") and "<think>" not in content:
        content = "<think>" + str(msg["thinking"]) + "</think>" + content
    return content


_THINK_RE = re.compile(r"<think>(.*?)</think>", re.S | re.I)


def _split_thinking(raw: str):
    """Return (thinking, reply) with <think>...</think> blocks removed from the reply."""
    thinking = "\n".join(m.strip() for m in _THINK_RE.findall(raw)).strip()
    reply = _THINK_RE.sub("", raw)
    if "</think>" in reply.lower():                     # unmatched closing tag
        head, _, reply = reply.partition("</think>")
        thinking = (thinking + "\n" + head).strip()
    return thinking, reply.strip()


def _parse_twin(raw: str) -> Dict[str, Any]:
    _, text = _split_thinking(raw)
    candidate = text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start:end + 1]
    try:
        obj = json.loads(candidate)
        if not isinstance(obj, dict) or not isinstance(obj.get("risks", []), list):
            raise ValueError("unexpected shape")
    except ValueError:
        return {"risks": [], "overall_risk": "UNKNOWN", "ai_reasoning": raw}
    risks = []
    for i, r in enumerate(obj.get("risks") or []):
        if not isinstance(r, dict):
            continue
        risks.append({
            "id": str(r.get("id") or f"R{i + 1}"),
            "severity": str(r.get("severity") or "UNKNOWN").upper(),
            "title": str(r.get("title") or ""),
            "description": str(r.get("description") or ""),
            "mitigation": str(r.get("mitigation") or ""),
        })
    return {"risks": risks,
            "overall_risk": str(obj.get("overall_risk") or "UNKNOWN").upper(),
            "ai_reasoning": str(obj.get("ai_reasoning") or "")}


def _summarise_packets(packets: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """Compact statistics of real packets (min/max/mean/last per numeric field)."""
    stats: Dict[str, Dict[str, Any]] = {}
    states: List[str] = []
    for fields in packets:
        for name, f in fields.items():
            val = f.get("value") if isinstance(f, dict) else f
            unit = f.get("unit") if isinstance(f, dict) else None
            if name == "mission_state":
                if isinstance(val, str) and (not states or states[-1] != val):
                    states.append(val)
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            s = stats.setdefault(name, {"unit": unit, "n": 0, "min": val, "max": val, "sum": 0.0})
            s["n"] += 1
            s["min"] = min(s["min"], val)
            s["max"] = max(s["max"], val)
            s["sum"] += val
            s["last"] = val
    out_fields = {}
    for name, s in stats.items():
        out_fields[name] = {"unit": s["unit"], "min": round(s["min"], 4), "max": round(s["max"], 4),
                            "mean": round(s["sum"] / s["n"], 4), "last": s["last"], "samples": s["n"]}
    return {"source": label, "packet_count": len(packets), "mission_states": states,
            "fields": out_fields}


def _flight_summary_for(flight_id: Any):
    """Summary for a flight id; falls back to latest completed flight, then live packets."""
    if flight_id is not None and _db is not None:
        try:
            fid = int(flight_id)
        except (TypeError, ValueError):
            return None, "flight_id must be an integer"
        with _db_lock:
            packets = _load_flight_packets(_db, fid)
        if packets:
            return _summarise_packets([p["fields"] for p in packets], f"recorded flight #{fid}"), None
    if _db is not None:
        with _db_lock:
            flights = _list_completed_flights(_db)
            packets = _load_flight_packets(_db, flights[-1]["id"]) if flights else None
        if packets:
            return _summarise_packets([p["fields"] for p in packets],
                                      f"recorded flight #{flights[-1]['id']}"), None
    live = list(_recent_packets)
    if live:
        return _summarise_packets(live, "live session (flight not yet completed)"), None
    return None, "No flight data yet — no packets received and no completed flights in the database."


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
    parser.add_argument("--sim", action="store_true",
                        help="Replay a recorded (completed) flight from the database instead of reading serial")
    parser.add_argument("--sim-flight", type=int, default=None,
                        help="With --sim: flight id to replay (skips the interactive picker)")
    args = parser.parse_args()

    global _db
    db_conn = _init_db(DB_PATH)
    _db = db_conn

    print("[AirOne] Starting AirOne CanSat Ground Station")
    print(f"[AirOne] Database: {DB_PATH}")
    print(f"[AirOne] Completed flights logged: {_count_completed_flights(db_conn)}")

    if args.sim:
        flights = _list_completed_flights(db_conn)
        if not flights:
            print("[AirOne] --sim: no completed flights in the database yet. "
                  "Record a real flight first (it completes when LANDED is received).")
            db_conn.close()
            return 1
        print("[AirOne] Recorded flights:")
        for n, f in enumerate(flights, 1):
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f["start_time"])) \
                if f["start_time"] else "?"
            print(f"  [{n}] flight #{f['id']}  {started}  {f['packet_count']} packets  "
                  f"{f['duration_s'] if f['duration_s'] is not None else '?'} s")
        chosen: Optional[int] = args.sim_flight
        while chosen is None:
            try:
                ans = input(f"Select flight [1-{len(flights)}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                db_conn.close()
                return 1
            if ans.isdigit() and 1 <= int(ans) <= len(flights):
                chosen = flights[int(ans) - 1]["id"]
            else:
                print("  invalid choice")
        with _state_lock:
            _state["mode"] = "sim"
        ok, err = _sim_load(chosen)
        if not ok:
            print(f"[AirOne] --sim: {err}")
            db_conn.close()
            return 1
        threading.Thread(target=_sim_thread, name="SimThread", daemon=True).start()
        print(f"[AirOne] SIM mode: replaying flight #{chosen} (serial port not opened)")
    else:
        print("[AirOne] Waiting for USB ground bridge... (Ctrl+C to stop)")
        t = threading.Thread(target=_serial_thread, args=(args,), name="SerialThread", daemon=True)
        t.start()

    print(f"[AirOne] Dashboard: http://localhost:{args.flask_port}")

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
