"""AirOne Ground Station — Flask Blueprint (dashboard + SSE stream).

Blueprint is registered by run.py.  It reads shared state via
``current_app.config`` so there are no circular imports:

    app.config['AIRONE_BUS']        – _SSEBus instance
    app.config['AIRONE_STATE']      – shared state dict
    app.config['AIRONE_STATE_LOCK'] – threading.Lock
"""
from __future__ import annotations

import json
import queue
import time
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify  # type: ignore

_STATIC = Path(__file__).parent / "static"

ui_bp = Blueprint("ui", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_bus():
    return current_app.config["AIRONE_BUS"]

def _get_state():
    return current_app.config["AIRONE_STATE"]

def _get_lock():
    return current_app.config["AIRONE_STATE_LOCK"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ui_bp.route("/")
def index():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html")


@ui_bp.route("/static/<path:filename>")
def static_file(filename):
    from flask import send_from_directory  # type: ignore
    return send_from_directory(str(_STATIC), filename)


@ui_bp.route("/stream")
def stream():
    """Server-Sent Events endpoint.  Each connected browser tab gets its own queue."""
    bus = _get_bus()
    state = _get_state()
    lock = _get_lock()
    q = bus.subscribe()

    def _generate():
        # Send current state immediately so the page renders before the next packet.
        with lock:
            snap = dict(state)
        yield "event: status\ndata: " + json.dumps(snap) + "\n\n"

        last_heartbeat = time.time()
        try:
            while True:
                # Heartbeat comment every 5 s to keep proxy connections alive.
                if time.time() - last_heartbeat > 5:
                    yield ": keepalive\n\n"
                    last_heartbeat = time.time()
                try:
                    yield q.get(timeout=1.0)
                except queue.Empty:
                    continue
        finally:
            # Runs when the browser disconnects (generator closed), not on return.
            bus.unsubscribe(q)

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
