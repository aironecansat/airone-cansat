"""Configuration & system view.

Shows the effective (secret-redacted) backend configuration and live health /
database metrics. Configuration is presented according to role separation:

* VIEWER / OPERATOR  — cannot read the full config (backend returns 403,
  which is shown explicitly here rather than hidden).
* ENGINEER / ADMIN   — see the redacted config tree.

The view is read-only by design; live config edits are an ENGINEER action
performed via the REST API and are intentionally not wired to a one-click
button in the operator GUI to avoid accidental changes.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from PyQt5.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from .. import theme
from ..api_client import GroundStationClient


class ConfigView(QWidget):
    def __init__(self, client: GroundStationClient, parent=None):
        super().__init__(parent)
        self._client = client
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        self.role_label = QLabel("role: unknown")
        self.role_label.setStyleSheet(f"color:{theme.FG_MUTED};")
        head.addWidget(self.role_label)
        head.addStretch(1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        root.addLayout(head)

        cfg_box = QGroupBox("Effective configuration (secrets redacted)")
        cv = QVBoxLayout(cfg_box)
        self.config_view = QTextEdit()
        self.config_view.setReadOnly(True)
        cv.addWidget(self.config_view)
        root.addWidget(cfg_box, 2)

        health_box = QGroupBox("Health & database")
        hv = QVBoxLayout(health_box)
        self.health_view = QTextEdit()
        self.health_view.setReadOnly(True)
        hv.addWidget(self.health_view)
        root.addWidget(health_box, 1)

    def refresh(self) -> None:
        self.role_label.setText(f"role: {self._client.role or 'unknown'}")
        cfg = self._client.get_config()
        if cfg.ok and isinstance(cfg.data, dict):
            self.config_view.setPlainText(json.dumps(cfg.data, indent=2, default=str))
        else:
            self.config_view.setPlainText(
                f"CONFIG UNAVAILABLE — {cfg.error or cfg.state}\n"
                f"(ENGINEER role required to read configuration.)"
            )
        self._update_health()

    def on_health(self, result) -> None:
        """Slot for periodic health poll."""
        self._render_health(result)

    def _update_health(self) -> None:
        self._render_health(self._client.health())

    def _render_health(self, result) -> None:
        if result.ok and isinstance(result.data, dict):
            self.health_view.setPlainText(json.dumps(result.data, indent=2, default=str))
        else:
            self.health_view.setPlainText(
                f"HEALTH UNAVAILABLE — {result.error or result.state}"
            )
