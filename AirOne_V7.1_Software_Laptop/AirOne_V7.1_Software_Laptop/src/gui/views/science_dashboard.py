"""Science dashboard.

Left: a searchable catalogue of the 37 scientific analyses grouped by
category. Selecting one shows its full, honest result card — value with
units, method, result-type, confidence, uncertainty, assumptions and
limitations (including the automatic causation caveat for correlations).

Right: a field-history plot with a raw / filtered / fused sensor toggle so an
operator can compare the provenance tiers of the same physical quantity.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from .. import theme
from ..api_client import ApiResult, GroundStationClient
from ..widgets import TimeSeriesPlot

# Provenance toggle: label -> candidate sensor_ids (first present wins).
PROVENANCE_SETS = {
    "Pressure": {
        "RAW": "bme688_pressure",
        "FILTERED": "bme688_pressure",
        "FUSED": "fused_pressure",
    },
    "Temperature": {
        "RAW": "bme688_temperature",
        "FILTERED": "bme688_temperature",
        "FUSED": "fused_temperature",
    },
    "Altitude": {
        "RAW": "gnss_altitude",
        "FILTERED": "gnss_altitude",
        "FUSED": "fused_altitude",
    },
}


class ScienceDashboardView(QWidget):
    def __init__(self, client: GroundStationClient, parent=None):
        super().__init__(parent)
        self._client = client
        self._catalogue: Dict[str, Any] = {}
        self._live_results: Dict[str, Any] = {}

        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        # --- left: searchable catalogue ----------------------------------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Analysis catalogue (searchable)"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("filter analyses by name or category…")
        self.search.textChanged.connect(self._apply_filter)
        lv.addWidget(self.search)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        lv.addWidget(self.list, 1)
        self.run_btn = QPushButton("Run selected on stored data (SCIENTIST+)")
        self.run_btn.clicked.connect(self._run_selected)
        lv.addWidget(self.run_btn)
        splitter.addWidget(left)

        # --- middle: result card -----------------------------------------
        mid = QGroupBox("Result")
        mv = QVBoxLayout(mid)
        self.result_view = QTextEdit()
        self.result_view.setReadOnly(True)
        mv.addWidget(self.result_view)
        splitter.addWidget(mid)

        # --- right: provenance plot ---------------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Quantity:"))
        self.quantity = QComboBox()
        self.quantity.addItems(list(PROVENANCE_SETS.keys()))
        self.quantity.currentTextChanged.connect(self._refresh_plot)
        ctl.addWidget(self.quantity)
        ctl.addWidget(QLabel("Provenance:"))
        self.provenance = QComboBox()
        self.provenance.addItems(["RAW", "FILTERED", "FUSED"])
        self.provenance.currentTextChanged.connect(self._refresh_plot)
        ctl.addWidget(self.provenance)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self._refresh_plot)
        ctl.addWidget(self.reload_btn)
        ctl.addStretch(1)
        rv.addLayout(ctl)
        self.plot = TimeSeriesPlot("Field history")
        rv.addWidget(self.plot, 1)
        self.plot_status = QLabel("")
        self.plot_status.setStyleSheet(f"color:{theme.FG_MUTED};")
        rv.addWidget(self.plot_status)
        splitter.addWidget(right)

        splitter.setSizes([260, 380, 460])
        root.addWidget(splitter)

    # -- catalogue loading --------------------------------------------------
    def load_catalogue(self) -> None:
        res = self._client.analysis_catalogue()
        if not res.ok or not isinstance(res.data, dict):
            self.result_view.setPlainText(
                f"Catalogue UNAVAILABLE — {res.error or res.state}"
            )
            return
        # describe() returns a list of spec dicts; index it by name.
        analyses = res.data.get("analyses", [])
        if isinstance(analyses, list):
            self._catalogue = {str(s.get("name")): s for s in analyses}
        elif isinstance(analyses, dict):
            self._catalogue = analyses
        else:
            self._catalogue = {}
        self._populate_list()

    def _populate_list(self) -> None:
        self.list.clear()
        # Group by category using the describe() payload.
        by_cat: Dict[str, list] = {}
        for name, spec in sorted(self._catalogue.items()):
            cat = str(spec.get("category", "other"))
            by_cat.setdefault(cat, []).append(name)
        for cat in sorted(by_cat):
            header = QListWidgetItem(f"— {cat.upper()} —")
            header.setFlags(Qt.NoItemFlags)
            header.setForeground(Qt.gray)
            self.list.addItem(header)
            for name in by_cat[cat]:
                self.list.addItem(QListWidgetItem(name))
        self._apply_filter(self.search.text())

    def _apply_filter(self, text: str) -> None:
        text = (text or "").strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            if not (item.flags() & Qt.ItemIsSelectable):
                item.setHidden(False)
                continue
            spec = self._catalogue.get(item.text(), {})
            hay = f"{item.text()} {spec.get('category','')} {spec.get('description','')}".lower()
            item.setHidden(bool(text) and text not in hay)

    # -- selection / results ------------------------------------------------
    def _current_name(self) -> Optional[str]:
        item = self.list.currentItem()
        if item is None or not (item.flags() & Qt.ItemIsSelectable):
            return None
        return item.text()

    def _on_select(self, *_a) -> None:
        name = self._current_name()
        if not name:
            return
        self._render_result(name)

    def on_results(self, result: ApiResult) -> None:
        """Slot for live results poll."""
        if result.ok and isinstance(result.data, dict):
            self._live_results = result.data.get("results", {}) or {}
        else:
            self._live_results = {}
        name = self._current_name()
        if name:
            self._render_result(name)

    def _render_result(self, name: str) -> None:
        spec = self._catalogue.get(name, {})
        live = self._live_results.get(name)
        lines = [f"# {name}", ""]
        lines.append(f"category: {spec.get('category','?')}")
        lines.append(f"description: {spec.get('description','')}")
        lines.append(f"required fields: {', '.join(spec.get('required_fields', []) or [])}")
        lines.append(f"min samples: {spec.get('min_samples','?')}")
        lines.append("")
        if not live:
            lines.append("LIVE RESULT: none yet (analysis has not produced a value)")
            self.result_view.setPlainText("\n".join(lines))
            return
        lines.append("── LIVE RESULT ──")
        lines.append(self._format_result(live))
        self.result_view.setPlainText("\n".join(lines))

    @staticmethod
    def _format_result(r: Dict[str, Any]) -> str:
        valid = r.get("valid", False)
        out = []
        if not valid:
            out.append("STATE: INVALID / INSUFFICIENT")
            lim = r.get("limitations") or []
            for reason in lim:
                out.append(f"  reason: {reason}")
            return "\n".join(out) or "INVALID"
        value = r.get("value")
        unit = r.get("unit", "")
        out.append(f"value: {value} {unit}".strip())
        out.append(f"result type: {r.get('result_type','?')}")
        out.append(f"method: {r.get('method','?')}")
        conf = r.get("confidence")
        if isinstance(conf, (int, float)):
            out.append(f"confidence: {conf:.2f}")
        unc = r.get("uncertainty")
        if unc not in (None, {}):
            out.append(f"uncertainty: {unc}")
        n = r.get("n_samples")
        if n is not None:
            out.append(f"n samples: {n}")
        assumptions = r.get("assumptions") or []
        if assumptions:
            out.append("assumptions:")
            out.extend(f"  - {a}" for a in assumptions)
        limitations = r.get("limitations") or []
        if limitations:
            out.append("limitations:")
            out.extend(f"  - {l}" for l in limitations)
        return "\n".join(out)

    def _run_selected(self) -> None:
        name = self._current_name()
        if not name:
            self.result_view.setPlainText("Select an analysis first.")
            return
        res = self._client.run_analysis(name)
        if not res.ok:
            self.result_view.setPlainText(
                f"Run failed — {res.error or res.state}\n"
                f"(SCIENTIST role required; on-demand needs stored data.)"
            )
            return
        result = (res.data or {}).get("result", {})
        self.result_view.setPlainText(
            f"# {name} (on-demand over stored window)\n\n" + self._format_result(result)
        )

    # -- provenance plot ----------------------------------------------------
    def _refresh_plot(self, *_a) -> None:
        quantity = self.quantity.currentText()
        prov = self.provenance.currentText()
        sensor_id = PROVENANCE_SETS.get(quantity, {}).get(prov)
        if not sensor_id:
            self.plot_status.setText("No sensor mapping for this selection.")
            return
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=6)
        res = self._client.history(
            sensor_id, start.isoformat(), end.isoformat(), limit=5000
        )
        if not res.ok or not isinstance(res.data, dict):
            self.plot.plot_series([], message=f"HISTORY UNAVAILABLE — {res.error or res.state}")
            self.plot_status.setText(f"history error: {res.error or res.state}")
            return
        rows = res.data.get("measurements", [])
        xs, ys = [], []
        t0 = None
        invalid = 0
        for m in rows:
            if not m.get("valid") or not isinstance(m.get("value"), (int, float)):
                invalid += 1
                continue
            v = m["value"]
            if isinstance(v, float) and math.isnan(v):
                invalid += 1
                continue
            try:
                dt = datetime.fromisoformat(str(m["timestamp"]).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            if t0 is None:
                t0 = dt
            xs.append((dt - t0).total_seconds())
            ys.append(v)
        color = theme.source_color(prov if prov != "FILTERED" else "FILTERED")
        self.plot.plot_series(
            [{"x": xs, "y": ys, "label": f"{sensor_id} [{prov}]", "color": color}],
            message=f"NO VALID {quantity.upper()} SAMPLES for {sensor_id}",
        )
        self.plot_status.setText(
            f"{sensor_id}: {len(ys)} valid, {invalid} invalid/missing over last 6h"
        )
