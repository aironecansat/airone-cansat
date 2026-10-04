"""Mission Control dashboard.

Always shows the primary and secondary mission objectives (design rule: these
panels are never hidden). Shows the latest telemetry table with per-quality
colour coding and a live altitude / key-sensor plot built from a rolling
buffer of polled values.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, Tuple

from PyQt5.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from .. import theme
from ..api_client import ApiResult
from ..widgets import (
    MissionObjectivePanel, QualityLegend, TelemetryTable, TimeSeriesPlot,
)

# Fields we try to trend live if present in telemetry.
TREND_FIELDS = [
    ("gnss_altitude", "#3fb950"),
    ("fused_altitude", "#58a6ff"),
    ("bme688_temperature", "#db6d28"),
    ("fused_temperature", "#d29922"),
    ("radiation_cpm", "#a371f7"),
]


class MissionControlView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._buffers: Dict[str, Deque[Tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=600)
        )
        self._t0 = None

        root = QVBoxLayout(self)

        # --- objectives row (always visible) -----------------------------
        obj_row = QHBoxLayout()
        self.primary = MissionObjectivePanel("PRIMARY MISSION")
        self.secondary = MissionObjectivePanel("SECONDARY MISSION")
        obj_row.addWidget(self.primary)
        obj_row.addWidget(self.secondary)
        root.addLayout(obj_row)

        # --- main split: telemetry table + live plots ---------------------
        mid = QGridLayout()
        tel_box = QGroupBox("Latest Telemetry (per sensor)")
        tv = QVBoxLayout(tel_box)
        self.table = TelemetryTable()
        tv.addWidget(self.table)
        tv.addWidget(QualityLegend())
        mid.addWidget(tel_box, 0, 0)

        plot_box = QGroupBox("Live Trends")
        pv = QVBoxLayout(plot_box)
        self.alt_plot = TimeSeriesPlot("Altitude / Environment (live)")
        pv.addWidget(self.alt_plot)
        mid.addWidget(plot_box, 0, 1)
        mid.setColumnStretch(0, 1)
        mid.setColumnStretch(1, 1)
        root.addLayout(mid, 1)

        self._quality_summary = QLabel("Quality: —")
        self._quality_summary.setStyleSheet(f"color:{theme.FG_MUTED};")
        root.addWidget(self._quality_summary)

    # -- slots --------------------------------------------------------------
    def on_mission(self, primary: ApiResult, secondary: ApiResult) -> None:
        self.primary.update_from(primary)
        self.secondary.update_from(secondary)

    def on_telemetry(self, result: ApiResult) -> None:
        self.table.update_from(result)
        if not result.ok or not isinstance(result.data, dict):
            return
        measurements = result.data.get("measurements", [])
        valid = sum(1 for m in measurements if m.get("valid"))
        invalid = len(measurements) - valid
        self._quality_summary.setText(
            f"Quality: {valid} valid / {invalid} invalid across {len(measurements)} sensors"
        )
        # Accumulate trend buffers.
        now = datetime.now(timezone.utc)
        if self._t0 is None:
            self._t0 = now
        t = (now - self._t0).total_seconds()
        by_field = {}
        for m in measurements:
            if m.get("valid") and isinstance(m.get("value"), (int, float)):
                v = m["value"]
                if isinstance(v, float) and math.isnan(v):
                    continue
                by_field[str(m.get("field_name"))] = v
        for field_name, _color in TREND_FIELDS:
            if field_name in by_field:
                self._buffers[field_name].append((t, by_field[field_name]))
        self._redraw()

    def _redraw(self) -> None:
        if not self.alt_plot.available:
            return
        series = []
        for field_name, color in TREND_FIELDS:
            buf = self._buffers.get(field_name)
            if buf:
                xs = [p[0] for p in buf]
                ys = [p[1] for p in buf]
                series.append({"x": xs, "y": ys, "label": field_name, "color": color})
        self.alt_plot.plot_series(series, message="NO TRENDABLE FIELDS YET")
