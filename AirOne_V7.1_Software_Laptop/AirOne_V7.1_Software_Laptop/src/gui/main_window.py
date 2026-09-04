"""Main ground-station window: status banner + tabbed views + poller wiring."""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMainWindow, QTabWidget, QVBoxLayout, QWidget

from . import theme
from .api_client import ApiResult, GroundStationClient
from .poller import DataPoller
from .views.ai_diagnostics import AIDiagnosticsView
from .views.config_view import ConfigView
from .views.map_view import MapView
from .views.mission_control import MissionControlView
from .views.presentation_mode import PresentationModeView
from .views.replay_view import ReplayView
from .views.science_dashboard import ScienceDashboardView
from .widgets import StatusBanner


class MainWindow(QMainWindow):
    def __init__(self, client: GroundStationClient, poll_interval_ms: int = 1000, parent=None):
        super().__init__(parent)
        self._client = client
        self.setWindowTitle("AirOne V7.1 — CanSat Ground Station")
        self.resize(1280, 820)
        self.setStyleSheet(theme.STYLESHEET)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        self.banner = StatusBanner()
        root.addWidget(self.banner)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        # Views.
        self.mission_control = MissionControlView()
        self.science = ScienceDashboardView(client)
        self.replay = ReplayView(client)
        self.ai = AIDiagnosticsView(client)
        self.presentation = PresentationModeView(client)
        self.config = ConfigView(client)
        self.map = MapView(client)

        self.tabs.addTab(self.mission_control, "Mission Control")
        self.tabs.addTab(self.science, "Science")
        self.tabs.addTab(self.map, "Map")
        self.tabs.addTab(self.replay, "Replay")
        self.tabs.addTab(self.ai, "AI Diagnostics")
        self.tabs.addTab(self.presentation, "Judge Mode")
        self.tabs.addTab(self.config, "Config / System")

        # Load static catalogue once (best effort).
        self.science.load_catalogue()

        # Poller.
        self._poller = DataPoller(client, interval_ms=poll_interval_ms)
        self._poller.telemetry_updated.connect(self.mission_control.on_telemetry)
        self._poller.mission_updated.connect(self.mission_control.on_mission)
        self._poller.mission_updated.connect(self._on_mission_state)
        self._poller.analysis_updated.connect(self.science.on_results)
        self._poller.health_updated.connect(self.config.on_health)
        self._poller.connection_changed.connect(self.banner.set_connection)
        self._poller.start()

        # F11 toggles a distraction-free judge mode.
        self.tabs.setCurrentWidget(self.mission_control)

    def _on_mission_state(self, primary: ApiResult, _secondary: ApiResult) -> None:
        if primary.ok and isinstance(primary.data, dict):
            state = str(primary.data.get("state", "BOOT"))
            self.banner.set_mission(state)
            self.banner.set_detail(f"role={self._client.role or '?'}")

    def keyPressEvent(self, event):  # noqa: N802 - Qt override
        if event.key() == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.tabs.setCurrentWidget(self.presentation)
                self.presentation.refresh()
                self.showFullScreen()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt override
        try:
            self._poller.stop()
            self._poller.wait(2000)
        finally:
            super().closeEvent(event)
