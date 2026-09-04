"""Mission replay.

Loads stored history for a chosen field and replays it with play / pause /
seek / speed controls and event-jump (start, apogee = max altitude, landing =
last valid). The replay works exclusively over persisted data; if nothing is
stored it says so explicitly rather than animating a fake trace.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from .. import theme
from ..api_client import GroundStationClient
from ..widgets import TimeSeriesPlot

REPLAY_FIELDS = [
    "fused_altitude", "gnss_altitude", "fused_temperature",
    "bme688_temperature", "fused_pressure", "radiation_cpm",
]


class ReplayView(QWidget):
    def __init__(self, client: GroundStationClient, parent=None):
        super().__init__(parent)
        self._client = client
        self._t: List[float] = []
        self._v: List[float] = []
        self._idx = 0

        root = QVBoxLayout(self)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Field:"))
        self.field = QComboBox()
        self.field.addItems(REPLAY_FIELDS)
        ctl.addWidget(self.field)
        self.load_btn = QPushButton("Load")
        self.load_btn.clicked.connect(self.load)
        ctl.addWidget(self.load_btn)
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self._toggle_play)
        ctl.addWidget(self.play_btn)
        ctl.addWidget(QLabel("Speed:"))
        self.speed = QComboBox()
        self.speed.addItems(["0.5x", "1x", "2x", "4x", "8x"])
        self.speed.setCurrentText("1x")
        ctl.addWidget(self.speed)
        self.jump_start = QPushButton("⏮ Start")
        self.jump_start.clicked.connect(lambda: self._seek_to(0))
        self.jump_apogee = QPushButton("⛰ Apogee")
        self.jump_apogee.clicked.connect(self._seek_apogee)
        self.jump_land = QPushButton("⏭ Landing")
        self.jump_land.clicked.connect(lambda: self._seek_to(max(0, len(self._t) - 1)))
        for b in (self.jump_start, self.jump_apogee, self.jump_land):
            ctl.addWidget(b)
        ctl.addStretch(1)
        root.addLayout(ctl)

        self.plot = TimeSeriesPlot("Replay")
        root.addWidget(self.plot, 1)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.valueChanged.connect(self._on_slider)
        root.addWidget(self.slider)

        self.readout = QLabel("No data loaded.")
        self.readout.setStyleSheet(f"color:{theme.FG_MUTED};")
        root.addWidget(self.readout)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def load(self) -> None:
        field = self.field.currentText()
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=12)
        res = self._client.history(field, start.isoformat(), end.isoformat(), limit=20000)
        self._t, self._v = [], []
        if not res.ok or not isinstance(res.data, dict):
            self.readout.setText(f"HISTORY UNAVAILABLE — {res.error or res.state}")
            self.plot.plot_series([], message="NO STORED DATA")
            self.slider.setMaximum(0)
            return
        rows = res.data.get("measurements", [])
        t0 = None
        for m in rows:
            if not m.get("valid") or not isinstance(m.get("value"), (int, float)):
                continue
            v = m["value"]
            if isinstance(v, float) and math.isnan(v):
                continue
            try:
                dt = datetime.fromisoformat(str(m["timestamp"]).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            if t0 is None:
                t0 = dt
            self._t.append((dt - t0).total_seconds())
            self._v.append(v)
        if not self._t:
            self.readout.setText(f"No valid stored samples for {field}.")
            self.plot.plot_series([], message="NO VALID SAMPLES")
            self.slider.setMaximum(0)
            return
        self.slider.setMaximum(len(self._t) - 1)
        self._seek_to(0)
        self.readout.setText(f"Loaded {len(self._t)} samples for {field}.")

    def _toggle_play(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.play_btn.setText("▶ Play")
        elif self._t:
            factor = float(self.speed.currentText().rstrip("x"))
            self._timer.start(int(max(20, 200 / factor)))
            self.play_btn.setText("⏸ Pause")

    def _tick(self) -> None:
        if self._idx >= len(self._t) - 1:
            self._timer.stop()
            self.play_btn.setText("▶ Play")
            return
        self._idx += 1
        self.slider.setValue(self._idx)

    def _seek_to(self, idx: int) -> None:
        if not self._t:
            return
        self._idx = max(0, min(idx, len(self._t) - 1))
        self.slider.setValue(self._idx)
        self._redraw()

    def _seek_apogee(self) -> None:
        if not self._v:
            return
        self._seek_to(int(max(range(len(self._v)), key=lambda i: self._v[i])))

    def _on_slider(self, value: int) -> None:
        self._idx = value
        self._redraw()

    def _redraw(self) -> None:
        if not self._t:
            return
        upto = self._idx + 1
        series = [{
            "x": self._t[:upto], "y": self._v[:upto],
            "label": self.field.currentText(), "color": theme.ACCENT,
        }]
        self.plot.plot_series(series, message="")
        # Cursor marker.
        if self.plot.available:
            self.plot._ax.axvline(self._t[self._idx], color=theme.quality_color("SIMULATED"),
                                  linewidth=0.8)
            self.plot._canvas.draw_idle()
        self.readout.setText(
            f"t={self._t[self._idx]:.1f}s  value={self._v[self._idx]:.4g}  "
            f"[{self._idx + 1}/{len(self._t)}]"
        )
