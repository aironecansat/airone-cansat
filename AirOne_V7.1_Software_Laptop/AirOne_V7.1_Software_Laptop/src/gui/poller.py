"""Background polling thread for the GUI.

Runs in a :class:`QThread` so the Qt event loop never blocks on network I/O.
Emits typed signals with :class:`ApiResult` payloads. Views connect to the
signals they care about and render explicit states on failure — they never
invent data when a poll fails.
"""
from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal

from .api_client import ApiResult, GroundStationClient


class DataPoller(QThread):
    """Polls the backend at a fixed cadence and emits results."""

    telemetry_updated = pyqtSignal(object)     # ApiResult
    mission_updated = pyqtSignal(object, object)  # (primary, secondary)
    analysis_updated = pyqtSignal(object)      # ApiResult
    health_updated = pyqtSignal(object)        # ApiResult
    connection_changed = pyqtSignal(str)       # ConnectionState value

    def __init__(self, client: GroundStationClient, interval_ms: int = 1000, parent=None):
        super().__init__(parent)
        self._client = client
        self._interval_ms = int(interval_ms)
        self._running = True
        self._last_state = None

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:  # noqa: D401 - QThread entry point
        while self._running:
            # Telemetry (fast path).
            tel = self._client.latest_telemetry()
            self.telemetry_updated.emit(tel)

            # Mission objectives.
            primary = self._client.mission_primary()
            secondary = self._client.mission_secondary()
            self.mission_updated.emit(primary, secondary)

            # Analyses + health at the same cadence (cheap, in-memory on server).
            self.analysis_updated.emit(self._client.analysis_results())
            self.health_updated.emit(self._client.health())

            # Emit connection-state changes only on transition.
            state = self._client.state.value
            if state != self._last_state:
                self._last_state = state
                self.connection_changed.emit(state)

            # Sleep in small slices so stop() is responsive.
            slept = 0
            while self._running and slept < self._interval_ms:
                self.msleep(50)
                slept += 50
