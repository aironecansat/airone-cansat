"""End-to-end smoke test for the ground-station GUI.

Spins up the *real* Flask API on an ephemeral port with simulated telemetry
persisted to a temp DB, then drives the real REST client and builds the real
Qt MainWindow (offscreen). This proves the GUI stack works against the live
backend — no mocked endpoints, no fabricated data.

The whole module is skipped cleanly if PyQt5 or requests are unavailable, so
CI on a headless box without Qt still passes.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("requests")

import threading  # noqa: E402
import time  # noqa: E402
from wsgiref.simple_server import make_server  # noqa: E402

from src.api.app import build_services, create_app  # noqa: E402
from tests.conftest import TEST_USERS, seed_test_users  # noqa: E402
from src.scientific import build_default_registry, SeriesBundle  # noqa: E402
from src.scientific.narrative import build_narrative  # noqa: E402
from src.simulation import FlightProfile, MissionSimulator  # noqa: E402
from src.workers.scientific_worker import ScientificAnalysisWorker  # noqa: E402


class _ServerThread(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self._srv = make_server("127.0.0.1", 0, app)
        self.port = self._srv.server_port

    def run(self):
        self._srv.serve_forever()

    def stop(self):
        self._srv.shutdown()


@pytest.fixture
def live_backend(temp_db_path):
    config = {"mission_id": "default", "api": {"port": 0}, "security": {"bcrypt_rounds": 4}}
    services = build_services(temp_db_path, config)
    seed_test_users(services)

    # Generate a simulated mission and persist its measurements.
    sim = MissionSimulator(profile=FlightProfile(apogee_m=1000.0),
                           sample_rate_hz=2.0, seed=7)
    frames = sim.collect()
    for frame in frames:
        ms = list(frame.measurements.values())
        if ms:
            services.telemetry_repo.store_measurements(ms, mission_id="default")

    # Populate the scientific worker so /analysis/results and /narrative work.
    registry = build_default_registry()
    series = SeriesBundle(frames)
    results = {n: registry.run(n, series) for n in registry.names()}
    worker = ScientificAnalysisWorker(
        __import__("queue").Queue(), analysis_repo=services.analysis_repo,
        registry=registry, persist=False,
    )
    worker._latest_results = results  # ScientificResult objects; API calls .to_dict()
    worker._latest_narrative = build_narrative(results, mission_state="LANDED",
                                               mission_id="default")
    services.scientific_worker = worker

    app = create_app(services, config)
    server = _ServerThread(app)
    server.start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{server.port}"
    server.stop()


def test_client_login_and_telemetry(live_backend):
    from src.gui.api_client import ConnectionState, GroundStationClient

    client = GroundStationClient(base_url=live_backend, username="admin",
                                 password=TEST_USERS["admin"][0])
    assert client.login().ok
    assert client.state is ConnectionState.CONNECTED

    tel = client.latest_telemetry()
    assert tel.ok
    measurements = tel.data["measurements"]
    assert measurements, "expected persisted simulated measurements"
    # Provenance must be present and honest (SIMULATED for sim data).
    assert any(m.get("quality") == "SIMULATED" for m in measurements)


def test_client_analysis_and_narrative(live_backend):
    from src.gui.api_client import GroundStationClient

    client = GroundStationClient(base_url=live_backend, username="admin",
                                 password=TEST_USERS["admin"][0])
    res = client.analysis_results()
    assert res.ok and res.data["state"] == "AVAILABLE"
    assert res.data["results"], "worker should expose computed results"

    nar = client.narrative()
    assert nar.ok
    # Judge-mode sections must all be present and the causation disclaimer intact.
    for key in ("question", "measurements", "result", "evidence",
                "interpretation", "correlations", "confidence",
                "limitations", "disclaimer"):
        assert key in nar.data, f"missing narrative section {key}"
    assert "causation" in nar.data["disclaimer"].lower()


def test_unauthenticated_client_reports_state(live_backend):
    from src.gui.api_client import ConnectionState, GroundStationClient

    client = GroundStationClient(base_url=live_backend, username="admin",
                                 password="wrong-password")
    result = client.login()
    assert not result.ok
    assert client.state is ConnectionState.UNAUTHENTICATED


def test_mainwindow_builds_against_live_backend(live_backend):
    from PyQt5.QtWidgets import QApplication

    from src.gui.api_client import GroundStationClient
    from src.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    client = GroundStationClient(base_url=live_backend, username="admin",
                                 password=TEST_USERS["admin"][0])
    client.login()
    window = MainWindow(client, poll_interval_ms=100000)
    # Drive the science + presentation views directly (no reliance on timer).
    window.science.load_catalogue()
    window.presentation.refresh()
    app.processEvents()
    assert window.tabs.count() == 7
    # The catalogue must have loaded the analyses.
    assert window.science.list.count() > 0
    window._poller.stop()
    window._poller.wait(1500)
    window.close()
