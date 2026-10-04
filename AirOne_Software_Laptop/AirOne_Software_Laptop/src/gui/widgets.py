"""Reusable GUI widgets shared across views.

Every widget follows the honesty rule: when data is missing or a request
failed, the widget shows an explicit textual state (e.g. "DISCONNECTED",
"NO DATA", "UNAVAILABLE") rather than a blank or a fabricated zero.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QSizePolicy,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme
from .api_client import ApiResult

# Matplotlib is optional; the plot widget degrades explicitly if it is absent.
try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    _MPL_OK = True
    _MPL_ERROR = ""
except Exception as exc:  # pragma: no cover
    _MPL_OK = False
    _MPL_ERROR = str(exc)


class StatePill(QLabel):
    """A small coloured pill label showing an explicit state string."""

    def __init__(self, text: str = "UNKNOWN", color: str = theme.FG_MUTED, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.set_state(text, color)

    def set_state(self, text: str, color: str) -> None:
        self.setText(text)
        self.setStyleSheet(
            f"background:{color}; color:#0b0e13; border-radius:9px;"
            f"padding:3px 10px; font-weight:bold;"
        )


class StatusBanner(QFrame):
    """Top-of-window banner: connection state + backend mission state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        self._title = QLabel("AirOne — Ground Station")
        self._title.setObjectName("Heading")
        self.conn_pill = StatePill("UNKNOWN")
        self.mission_pill = StatePill("BOOT")
        self.detail = QLabel("")
        self.detail.setStyleSheet(f"color:{theme.FG_MUTED};")
        lay.addWidget(self._title)
        lay.addStretch(1)
        lay.addWidget(self.detail)
        lay.addWidget(QLabel("Link:"))
        lay.addWidget(self.conn_pill)
        lay.addWidget(QLabel("Mission:"))
        lay.addWidget(self.mission_pill)

    def set_connection(self, state: str) -> None:
        self.conn_pill.set_state(state, theme.connection_color(state))

    def set_mission(self, state: str) -> None:
        self.mission_pill.set_state(state, theme.mission_color(state))

    def set_detail(self, text: str) -> None:
        self.detail.setText(text)


class MissionObjectivePanel(QGroupBox):
    """Always-visible primary/secondary mission objective panel."""

    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        lay = QVBoxLayout(self)
        self.name = QLabel("—")
        self.name.setStyleSheet("font-size:16px; font-weight:bold;")
        self.state_pill = StatePill("UNKNOWN")
        row = QHBoxLayout()
        row.addWidget(QLabel("Phase:"))
        row.addWidget(self.state_pill)
        row.addStretch(1)
        self.status = QLabel("status: unknown")
        self.status.setStyleSheet(f"color:{theme.FG_MUTED};")
        lay.addWidget(self.name)
        lay.addLayout(row)
        lay.addWidget(self.status)

    def update_from(self, result: ApiResult) -> None:
        if not result.ok or not isinstance(result.data, dict):
            self.name.setText("UNAVAILABLE")
            self.status.setText(f"reason: {result.error or 'no data'}")
            self.state_pill.set_state("UNAVAILABLE",
                                      theme.connection_color("DISCONNECTED"))
            return
        d = result.data
        self.name.setText(str(d.get("name", "—")))
        st = str(d.get("state", "UNKNOWN"))
        self.state_pill.set_state(st, theme.mission_color(st))
        self.status.setText(f"status: {d.get('status', 'unknown')}")


class TelemetryTable(QTableWidget):
    """Latest-per-sensor telemetry with per-row quality colour coding."""

    COLS = ["Field", "Sensor", "Value", "Unit", "Quality", "Source", "±Unc", "Age"]

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels(self.COLS)
        self.horizontalHeader().setStretchLastSection(True)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.verticalHeader().setVisible(False)
        self._empty_reason = "waiting for telemetry…"

    def update_from(self, result: ApiResult) -> None:
        if not result.ok or not isinstance(result.data, dict):
            self._show_message(f"NO TELEMETRY — {result.error or result.state or 'unavailable'}")
            return
        measurements = result.data.get("measurements", [])
        if not measurements:
            self._show_message("NO TELEMETRY — backend has no measurements yet")
            return
        self.setRowCount(len(measurements))
        for r, m in enumerate(measurements):
            q = str(m.get("quality", "MISSING"))
            value = m.get("value")
            valid = bool(m.get("valid", False))
            display_val = (
                f"{value:.4g}" if isinstance(value, (int, float)) and value is not None
                and not (isinstance(value, float) and math.isnan(value)) else "—"
            )
            unc = m.get("uncertainty")
            unc_disp = (
                f"{unc:.3g}" if isinstance(unc, (int, float)) and unc is not None
                and math.isfinite(unc) else "∞" if unc == float("inf") else "—"
            )
            cells = [
                str(m.get("field_name", "")),
                str(m.get("sensor_id", "")),
                display_val if valid else "INVALID",
                str(m.get("unit", "")),
                q,
                str(m.get("source", "")),
                unc_disp,
                self._age(m.get("timestamp")),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 4:  # quality column
                    item.setForeground(QColor(theme.quality_color(q)))
                if c == 5:  # source column
                    item.setForeground(QColor(theme.source_color(str(m.get("source", "")))))
                if not valid and c == 2:
                    item.setForeground(QColor(theme.quality_color("INVALID")))
                self.setItem(r, c, item)

    def _age(self, ts: Optional[str]) -> str:
        if not ts:
            return "—"
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            secs = (datetime.now(timezone.utc) - dt).total_seconds()
            return f"{secs:.1f}s"
        except Exception:  # noqa: BLE001
            return "—"

    def _show_message(self, message: str) -> None:
        self.setRowCount(1)
        item = QTableWidgetItem(message)
        item.setForeground(QColor(theme.FG_MUTED))
        self.setItem(0, 0, item)
        for c in range(1, len(self.COLS)):
            self.setItem(0, c, QTableWidgetItem(""))


class QualityLegend(QWidget):
    """A compact legend explaining the quality colour code."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Quality:"))
        for name, color in theme.QUALITY_COLORS.items():
            pill = QLabel(name)
            pill.setStyleSheet(
                f"color:#0b0e13; background:{color}; border-radius:7px;"
                f"padding:1px 6px; margin:1px; font-size:10px;"
            )
            lay.addWidget(pill)
        lay.addStretch(1)


class TimeSeriesPlot(QWidget):
    """A matplotlib time-series canvas that degrades explicitly if MPL is absent."""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if not _MPL_OK:
            msg = QLabel(f"PLOTTING UNAVAILABLE — matplotlib import failed:\n{_MPL_ERROR}")
            msg.setAlignment(Qt.AlignCenter)
            msg.setStyleSheet(f"color:{theme.quality_color('INVALID')};")
            lay.addWidget(msg)
            self._canvas = None
            return
        self._figure = Figure(figsize=(5, 3), facecolor=theme.BG_PANEL)
        self._canvas = FigureCanvas(self._figure)
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self._canvas)
        self._ax = self._figure.add_subplot(111)
        self._style_axes()

    def _style_axes(self) -> None:
        ax = self._ax
        ax.set_facecolor(theme.BG_PANEL)
        for spine in ax.spines.values():
            spine.set_color(theme.GRID)
        ax.tick_params(colors=theme.FG_MUTED, labelsize=8)
        ax.grid(True, color=theme.GRID, linewidth=0.4)
        if self._title:
            ax.set_title(self._title, color=theme.FG, fontsize=10)

    @property
    def available(self) -> bool:
        return self._canvas is not None

    def plot_series(self, series: List[Dict[str, Any]], message: str = "") -> None:
        """Render one or more series.

        ``series`` is a list of dicts: {x, y, label, color}. If empty, an
        explicit message is drawn instead of an empty plot.
        """
        if not self.available:
            return
        self._ax.clear()
        self._style_axes()
        drawn = False
        for s in series:
            xs, ys = s.get("x", []), s.get("y", [])
            if xs and ys:
                self._ax.plot(xs, ys, label=s.get("label", ""),
                              color=s.get("color", theme.ACCENT), linewidth=1.2)
                drawn = True
        if not drawn:
            self._ax.text(0.5, 0.5, message or "NO DATA",
                          transform=self._ax.transAxes, ha="center", va="center",
                          color=theme.FG_MUTED, fontsize=12)
        else:
            leg = self._ax.legend(loc="best", fontsize=8, facecolor=theme.BG_RAISED,
                                  edgecolor=theme.GRID)
            if leg:
                for txt in leg.get_texts():
                    txt.set_color(theme.FG)
        self._canvas.draw_idle()
