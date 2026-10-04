"""GNSS ground-track map.

Plots the CanSat's latitude/longitude ground track from stored GNSS history,
coloured by altitude so ascent/descent are visually separable. Uses a plain
2-D matplotlib axis (no online tiles required, so it works fully offline).

If GNSS data is absent it says so explicitly and never draws a fake track.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from .. import theme
from ..api_client import GroundStationClient

try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    _MPL_OK = True
    _MPL_ERROR = ""
except Exception as exc:  # pragma: no cover
    _MPL_OK = False
    _MPL_ERROR = str(exc)


class MapView(QWidget):
    def __init__(self, client: GroundStationClient, parent=None):
        super().__init__(parent)
        self._client = client
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        head.addWidget(QLabel("GNSS Ground Track (coloured by altitude)"))
        head.addStretch(1)
        self.refresh_btn = QPushButton("Reload track")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        root.addLayout(head)

        if not _MPL_OK:
            msg = QLabel(f"MAP UNAVAILABLE — matplotlib import failed:\n{_MPL_ERROR}")
            msg.setStyleSheet(f"color:{theme.quality_color('INVALID')};")
            root.addWidget(msg)
            self._canvas = None
        else:
            self._figure = Figure(figsize=(5, 5), facecolor=theme.BG_PANEL)
            self._canvas = FigureCanvas(self._figure)
            root.addWidget(self._canvas, 1)
            self._ax = self._figure.add_subplot(111)

        self.status = QLabel("Press 'Reload track'.")
        self.status.setStyleSheet(f"color:{theme.FG_MUTED};")
        root.addWidget(self.status)

    def _fetch(self, field: str) -> List[Tuple[str, float]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=12)
        res = self._client.history(field, start.isoformat(), end.isoformat(), limit=20000)
        out: List[Tuple[str, float]] = []
        if not res.ok or not isinstance(res.data, dict):
            return out
        for m in res.data.get("measurements", []):
            if m.get("valid") and isinstance(m.get("value"), (int, float)):
                v = m["value"]
                if isinstance(v, float) and math.isnan(v):
                    continue
                out.append((str(m.get("timestamp")), v))
        return out

    def refresh(self) -> None:
        if self._canvas is None:
            return
        lat = dict(self._fetch("gnss_lat"))
        lon = dict(self._fetch("gnss_lon"))
        alt = dict(self._fetch("gnss_altitude"))
        # Join on common timestamps.
        common = sorted(set(lat) & set(lon))
        self._ax.clear()
        self._ax.set_facecolor(theme.BG_PANEL)
        for spine in self._ax.spines.values():
            spine.set_color(theme.GRID)
        self._ax.tick_params(colors=theme.FG_MUTED, labelsize=8)
        self._ax.set_xlabel("Longitude", color=theme.FG_MUTED, fontsize=9)
        self._ax.set_ylabel("Latitude", color=theme.FG_MUTED, fontsize=9)

        if not common:
            self._ax.text(0.5, 0.5, "NO GNSS TRACK — no concurrent lat/lon samples stored",
                          transform=self._ax.transAxes, ha="center", va="center",
                          color=theme.FG_MUTED)
            self._canvas.draw_idle()
            self.status.setText("No GNSS data stored.")
            return
        xs = [lon[t] for t in common]
        ys = [lat[t] for t in common]
        cs = [alt.get(t, float("nan")) for t in common]
        have_alt = any(not (isinstance(c, float) and math.isnan(c)) for c in cs)
        self._ax.plot(xs, ys, color=theme.GRID, linewidth=0.6, zorder=1)
        if have_alt:
            sc = self._ax.scatter(xs, ys, c=cs, cmap="viridis", s=12, zorder=2)
            cb = self._figure.colorbar(sc, ax=self._ax, fraction=0.046, pad=0.04)
            cb.set_label("altitude (m)", color=theme.FG_MUTED, fontsize=8)
            cb.ax.tick_params(colors=theme.FG_MUTED, labelsize=7)
        else:
            self._ax.scatter(xs, ys, c=theme.ACCENT, s=12, zorder=2)
        # Start/end markers.
        self._ax.scatter([xs[0]], [ys[0]], c=theme.quality_color("VALID"),
                         s=60, marker="^", label="start", zorder=3)
        self._ax.scatter([xs[-1]], [ys[-1]], c=theme.quality_color("INVALID"),
                         s=60, marker="v", label="last", zorder=3)
        leg = self._ax.legend(loc="best", fontsize=8, facecolor=theme.BG_RAISED,
                              edgecolor=theme.GRID)
        if leg:
            for txt in leg.get_texts():
                txt.set_color(theme.FG)
        self._ax.set_aspect("equal", adjustable="datalim")
        self._canvas.draw_idle()
        self.status.setText(
            f"{len(common)} track points; altitude colouring={'on' if have_alt else 'off'}."
        )
