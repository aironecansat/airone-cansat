"""Main orchestrator: ordered startup and shutdown of all workers.

Every worker owns its own stop event. The orchestrator starts them in a defined
order and shuts them down in reverse, giving each up to 5 seconds before a
forced stop. The final state is written to ``data/last_shutdown_state.json``.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..api.app import ServiceContainer, create_app
from ..communication.serial_transport import SerialTransport
from ..core.events import get_event_bus
from ..data_processing.pipeline import PipelineContext
from ..telemetry import protocol
from .base_worker import BaseWorker
from .health_monitor import HealthMonitorWorker
from .packet_processor import PacketProcessorWorker
from .persistence_worker import PersistenceWorker
from .scientific_worker import ScientificAnalysisWorker
from .telemetry_receiver import TelemetryReceiverWorker
from .ml_worker import MLAnalysisWorker

logger = logging.getLogger(__name__)


DEFAULT_API_HOST = "127.0.0.1"


def resolve_api_host(api_cfg: Dict[str, Any]) -> str:
    """Resolve the API bind address.

    Precedence: ``AIRONE_API_HOST`` env var > ``api.host`` config > loopback.
    Binding to all interfaces (``0.0.0.0`` / ``::``) is allowed but logged as
    a warning because the API speaks plain HTTP — put a TLS reverse proxy in
    front of it when exposing it beyond the local machine.
    """

    host = os.environ.get("AIRONE_API_HOST") or api_cfg.get("host") or DEFAULT_API_HOST
    host = str(host).strip()
    if host in ("0.0.0.0", "::", ""):
        logger.warning(
            "API bound to ALL interfaces (%s): plain HTTP is exposed to the network. "
            "Use a TLS reverse proxy or bind to 127.0.0.1.", host or "0.0.0.0",
        )
    return host or DEFAULT_API_HOST


class FlaskAPIThread(BaseWorker):
    """Runs the Flask development server on its own thread."""

    def __init__(self, app, host: str = DEFAULT_API_HOST, port: int = 5000) -> None:
        super().__init__("FlaskAPI")
        self.app = app
        self.host = host
        self.port = port
        self._server = None

    def start(self) -> None:  # override to use werkzeug server we can shut down
        from werkzeug.serving import WSGIRequestHandler, make_server

        class _QuietHandler(WSGIRequestHandler):
            """Do not advertise the server stack (Werkzeug/Python versions)."""

            server_version = "AirOne"
            sys_version = ""

            def version_string(self) -> str:  # noqa: D401
                return "AirOne"

        self._server = make_server(
            self.host, self.port, self.app, threaded=True, request_handler=_QuietHandler
        )
        self._thread = threading.Thread(target=self._serve, name=self.name, daemon=True)
        self.health_status["state"] = "running"
        self._thread.start()
        logger.info("Flask API listening on %s:%d", self.host, self.port)

    def _serve(self) -> None:
        try:
            self._server.serve_forever()
        except Exception:  # noqa: BLE001
            logger.exception("Flask server crashed")

    def stop(self, timeout: float = 5.0) -> bool:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.health_status["state"] = "stopped"
        return True


class Orchestrator:
    def __init__(
        self,
        services: ServiceContainer,
        config: Dict[str, Any],
    ) -> None:
        self.services = services
        self.config = config
        self.event_bus = get_event_bus()

        # Queues connecting the workers.
        self.rx_queue: "queue.Queue" = queue.Queue(maxsize=10000)
        self.packet_queue: "queue.Queue" = queue.Queue(maxsize=10000)
        self.persist_queue: "queue.Queue" = queue.Queue(maxsize=10000)
        self.scientific_queue: "queue.Queue" = queue.Queue(maxsize=10000)
        self.ml_queue: "queue.Queue" = queue.Queue(maxsize=10000)

        # -- Telemetry link authentication (optional, explicit) ------------
        # AIRONE_LINK_KEY (hex, >= 16 bytes) enables verification of the
        # truncated HMAC-SHA256 frame tag. telemetry.require_authenticated_frames
        # makes unauthenticated frames INVALID instead of merely SUSPECT.
        self.telemetry_cfg = config.get("telemetry", {}) or {}
        self.link_key = protocol.parse_link_key(os.environ.get("AIRONE_LINK_KEY"))
        self.require_auth = bool(self.telemetry_cfg.get("require_authenticated_frames", False))
        if self.require_auth and self.link_key is None:
            raise ValueError(
                "telemetry.require_authenticated_frames=true but AIRONE_LINK_KEY is not set "
                "(hex string, at least 16 bytes)"
            )
        if self.link_key is None:
            self.link_auth_mode = "NOT_CONFIGURED"
        else:
            self.link_auth_mode = "REQUIRED" if self.require_auth else "OPTIONAL"
        logger.info("Telemetry link authentication: %s", self.link_auth_mode)

        # Pipeline context wired to the downstream queues.
        self.persistence_worker = PersistenceWorker(
            self.persist_queue, services.telemetry_repo
        )
        pipeline_ctx = PipelineContext(
            config=config.get("pipeline", {}),
            hmac_key=self.link_key,
            require_auth=self.require_auth,
            event_bus=self.event_bus,
            persistence_sink=self.persistence_worker.enqueue,
            scientific_sink=lambda f: self._safe_put(self.scientific_queue, f),
            ml_sink=lambda f: self._safe_put(self.ml_queue, f),
        )

        self.workers: List[BaseWorker] = []
        self.receiver = TelemetryReceiverWorker(
            self.rx_queue, self.packet_queue,
            link_key=self.link_key, require_auth=self.require_auth,
        )
        self.processor = PacketProcessorWorker(self.packet_queue, pipeline_ctx)
        sci_cfg = config.get("scientific", {})
        self.scientific = ScientificAnalysisWorker(
            self.scientific_queue,
            analysis_repo=getattr(services, "analysis_repo", None),
            window_size=int(sci_cfg.get("window_size", 240)),
            run_interval_s=float(sci_cfg.get("run_interval_s", 3.0)),
            mission_id_provider=lambda: str(config.get("mission_id", "default")),
            mission_state_provider=lambda: services.mission_machine.state.value,
            persist=bool(sci_cfg.get("persist", True)),
        )
        # Expose the scientific worker to the API layer for live results.
        services.scientific_worker = self.scientific
        ml_cfg = config.get("ml", {})
        self.ml = MLAnalysisWorker(
            self.ml_queue,
            window_size=int(ml_cfg.get("window_size", 240)),
            run_interval_s=float(ml_cfg.get("run_interval_s", 5.0)),
            features=ml_cfg.get("features"),
            min_samples=int(ml_cfg.get("min_samples", 20)),
            auto_fit=bool(ml_cfg.get("auto_fit", True)),
        )
        # Expose the ML worker to the API layer for advisory anomaly summaries.
        # (ML is advisory-only and holds no reference to the mission machine.)
        services.ml_worker = self.ml
        self.health = HealthMonitorWorker(
            services.db, queue_depth_provider=lambda: self.packet_queue.qsize()
        )

        api_cfg = config.get("api", {}) or {}
        self.app = create_app(services, config)
        host = resolve_api_host(api_cfg)
        self.api = FlaskAPIThread(
            self.app,
            host=host,
            port=int(api_cfg.get("port", 5000)),
        )

        # -- Real telemetry link (LoRa E22 over USB serial) --------------
        # The transport shares the orchestrator's rx_queue, so live radio bytes
        # travel the identical parser -> pipeline -> workers path as any other
        # source. It is created here but only connected in start().
        self.transport = SerialTransport(self.rx_queue)
        # Expose transport health to the API/GUI layer.
        services.serial_transport = self.transport
        # Explicit, honest link state. Never implies data when there is none.
        self.telemetry_link_status: str = "NOT_STARTED"
        services.telemetry_link_status = lambda: self.telemetry_link_status

        self._started = False

    @staticmethod
    def _safe_put(q: "queue.Queue", item) -> None:
        try:
            q.put_nowait(item)
        except queue.Full:
            logger.warning("Queue full; dropping frame")

    def start(self) -> None:
        # Startup order: persistence & analysis sinks first, then producers.
        self.persistence_worker.start()
        self.scientific.start()
        self.ml.start()
        self.processor.start()
        self.receiver.start()
        self.health.start()
        self.api.start()
        self.workers = [
            self.receiver, self.processor, self.persistence_worker,
            self.scientific, self.ml, self.health,
        ]
        self._started = True
        logger.info("Orchestrator started %d workers + API", len(self.workers))
        # Bring up the real telemetry link last, once consumers are ready.
        self.start_telemetry_link()

    def start_telemetry_link(self) -> str:
        """Open the LoRa/serial link and start streaming live telemetry.

        Returns the resulting link status string. This method NEVER fabricates
        telemetry: if the link cannot be opened it sets an explicit
        ``NOT_CONFIGURED`` state and returns without feeding any data. Use
        ``--simulate`` for isolated testing when no hardware is present.
        """

        cfg = self.telemetry_cfg
        if not bool(cfg.get("enabled", True)):
            self.telemetry_link_status = "DISABLED (telemetry.enabled=false)"
            logger.warning("Telemetry link DISABLED via config; no live data.")
            return self.telemetry_link_status

        port = cfg.get("serial_port")  # None => auto-discover
        baud = int(cfg.get("baud_rate", 115200))
        force_baud = bool(cfg.get("force_baud", False))

        ok = self.transport.connect(port=port, baud=baud, force_baud=force_baud)
        if not ok:
            where = port or "auto-discovery"
            self.telemetry_link_status = (
                f"NOT_CONFIGURED (no CanSat radio found via {where}; "
                f"not fabricating data — use --simulate for isolated testing)"
            )
            logger.warning("TELEMETRY %s", self.telemetry_link_status)
            return self.telemetry_link_status

        self.transport.start_receiver()
        h = self.transport.health
        self.telemetry_link_status = f"LIVE ({h.port} @ {h.baud} baud)"
        logger.info("TELEMETRY %s", self.telemetry_link_status)
        return self.telemetry_link_status

    def inject(self, data: bytes) -> None:
        """Feed raw bytes into the receiver (used by tests / simulation)."""

        self.rx_queue.put(data)

    def shutdown(self) -> None:
        if not self._started:
            return
        logger.info("Orchestrator shutting down (reverse order)")
        # Stop the live radio link first so no new bytes enter the pipeline.
        try:
            self.transport.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("Error disconnecting telemetry transport")
        self.telemetry_link_status = "STOPPED"
        # Reverse order: API -> ML -> Scientific -> Persistence -> Processor -> Receiver.
        self.api.stop(timeout=5.0)
        for w in (self.ml, self.scientific, self.persistence_worker, self.processor, self.receiver, self.health):
            w.stop(timeout=5.0)
        self._save_shutdown_state()
        self._started = False

    def _save_shutdown_state(self) -> None:
        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mission_state": self.services.mission_machine.state.value,
            "workers": {
                w.name: dict(w.health_status) for w in
                [self.receiver, self.processor, self.persistence_worker,
                 self.scientific, self.ml, self.health]
            },
        }
        path = os.path.join(self.config.get("data_dir", "data"), "last_shutdown_state.json")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        logger.info("Saved shutdown state to %s", path)

    def health_report(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {w.name: dict(w.health_status) for w in self.workers}
        h = self.transport.health
        report["TelemetryLink"] = {
            "status": self.telemetry_link_status,
            "auth_mode": self.link_auth_mode,
            "connected": h.connected,
            "port": h.port,
            "baud": h.baud,
            "bytes_received": h.bytes_received,
            "packets_received": h.packets_received,
            "crc_errors": h.crc_errors,
            "reconnect_count": h.reconnect_count,
        }
        return report
