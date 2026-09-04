"""AI / ML diagnostics.

Shows the ML tier honestly across four panels:
  * a permanent advisory banner (ML can NEVER command a mission-state change);
  * detector availability (which algorithms are usable and why);
  * the live advisory anomaly summary (or an explicit NOT_CONFIGURED state);
  * the model registry with provenance.

Nothing is fabricated: if the ML tier is not running or no model is trained,
that is stated explicitly and no anomaly scores are invented.
"""
from __future__ import annotations

from typing import Any, Dict, List

from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import theme
from ..api_client import GroundStationClient


class AIDiagnosticsView(QWidget):
    COLS = ["ID", "Name", "Type", "Version", "Active", "SHA-256", "Created At"]

    def __init__(self, client: GroundStationClient, parent=None):
        super().__init__(parent)
        self._client = client
        root = QVBoxLayout(self)

        banner = QLabel(
            "⚠  ADVISORY ONLY — The AI/ML tier informs operators. It CANNOT "
            "command mission-state changes. Lower tiers (raw telemetry, "
            "science) remain authoritative and are never overridden by ML."
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            f"background:{theme.QUALITY_COLORS['ESTIMATED']}; color:#0b0e13;"
            f"padding:8px; border-radius:5px; font-weight:bold;"
        )
        root.addWidget(banner)

        # --- controls -----------------------------------------------------
        head = QHBoxLayout()
        head.addWidget(QLabel("Model type:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(
            ["ensemble", "robust_zscore", "isolation_forest", "gmm", "lstm_forecast"]
        )
        head.addWidget(self.model_combo)
        self.train_btn = QPushButton("Train on stored telemetry")
        self.train_btn.clicked.connect(self._train)
        head.addWidget(self.train_btn)
        head.addStretch(1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        root.addLayout(head)

        # --- detector availability + live anomaly summary -----------------
        panels = QHBoxLayout()
        det_box = QGroupBox("Detector availability")
        det_layout = QVBoxLayout(det_box)
        self.detectors_label = QLabel("Press Refresh to probe detectors.")
        self.detectors_label.setWordWrap(True)
        det_layout.addWidget(self.detectors_label)
        panels.addWidget(det_box, 1)

        anom_box = QGroupBox("Live advisory anomaly summary")
        anom_layout = QVBoxLayout(anom_box)
        self.anomaly_label = QLabel("Press Refresh to query the ML worker.")
        self.anomaly_label.setWordWrap(True)
        anom_layout.addWidget(self.anomaly_label)
        panels.addWidget(anom_box, 1)
        root.addLayout(panels)

        # --- model registry ----------------------------------------------
        root.addWidget(QLabel("Model Registry"))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        self.status = QLabel("Press Refresh to query the ML tier.")
        self.status.setStyleSheet(f"color:{theme.FG_MUTED};")
        root.addWidget(self.status)

    # ----------------------------------------------------------------------
    def _train(self) -> None:
        model_type = self.model_combo.currentText()
        self.status.setText(f"Training '{model_type}' on stored telemetry…")
        res = self._client.ml_train(model_type=model_type)
        if not res.ok:
            reason = res.error or res.state or "unknown error"
            self.status.setText(f"TRAINING NOT COMPLETED — {reason}")
            self.refresh()
            return
        data = res.data or {}
        self.status.setText(
            f"Trained '{model_type}' — model_id={data.get('model_id')}, "
            f"samples={data.get('n_samples')}, sha256={str(data.get('sha256_hash',''))[:12]}…"
        )
        self.refresh()

    def refresh(self) -> None:
        self._refresh_detectors()
        self._refresh_anomalies()
        self._refresh_models()

    def _refresh_detectors(self) -> None:
        res = self._client.ml_detectors()
        if not res.ok:
            self.detectors_label.setText(
                f"UNAVAILABLE — {res.error or res.state} (SCIENTIST role required)."
            )
            return
        detectors: Dict[str, Any] = (res.data or {}).get("detectors", {})
        lines = []
        for name, info in detectors.items():
            mark = "✓" if info.get("available") else "✗"
            reason = "" if info.get("available") else f" — {info.get('reason', '')}"
            lines.append(f"{mark} {name}{reason}")
        self.detectors_label.setText("\n".join(lines) or "No detectors reported.")

    def _refresh_anomalies(self) -> None:
        res = self._client.ml_anomalies()
        if not res.ok:
            state = (res.data or {}).get("state", res.state or "UNAVAILABLE")
            self.anomaly_label.setText(
                f"ML tier state: {state}\n{res.error or ''}\n"
                "(No anomaly scores fabricated.)"
            )
            return
        data = res.data or {}
        state = data.get("state", "UNKNOWN")
        n_anom = data.get("n_anomalies", data.get("last_anomaly_count", "n/a"))
        n_samples = data.get("n_samples", "n/a")
        note = data.get("advisory_note", "")
        self.anomaly_label.setText(
            f"State: {state}\nSamples in window: {n_samples}\n"
            f"Flagged anomalies: {n_anom}\n{note}"
        )

    def _refresh_models(self) -> None:
        res = self._client.ml_models()
        if not res.ok:
            self.table.setRowCount(0)
            self.status.setText(
                f"ML REGISTRY UNAVAILABLE — {res.error or res.state} "
                f"(SCIENTIST role required)."
            )
            return
        models: List[Dict[str, Any]] = (res.data or {}).get("models", [])
        if not models:
            self.table.setRowCount(1)
            item = QTableWidgetItem("NO MODELS REGISTERED — ML tier idle (nothing trained yet)")
            item.setForeground(QColor(theme.FG_MUTED))
            self.table.setItem(0, 0, item)
            for c in range(1, len(self.COLS)):
                self.table.setItem(0, c, QTableWidgetItem(""))
            self.status.setText("Registry reachable; 0 models. No anomaly scores fabricated.")
            return
        self.table.setRowCount(len(models))
        for r, m in enumerate(models):
            cells = [
                str(m.get("id", m.get("model_id", ""))),
                str(m.get("name", "")),
                str(m.get("model_type", "")),
                str(m.get("version", "")),
                "yes" if m.get("is_active") else "no",
                str(m.get("sha256_hash", ""))[:16],
                str(m.get("created_at", "")),
            ]
            for c, text in enumerate(cells):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.status.setText(f"{len(models)} model(s) registered.")
