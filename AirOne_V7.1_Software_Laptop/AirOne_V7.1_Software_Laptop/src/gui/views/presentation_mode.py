"""Judge / Presentation mode.

Renders the mission narrative in the required didactic order:
QUESTION → MEASUREMENTS → RESULT → EVIDENCE → INTERPRETATION →
CORRELATIONS → CONFIDENCE → LIMITATIONS → UNAVAILABLE → DISCLAIMER.

This view is deliberately large-type and low-clutter for presenting to
judges. It renders exactly what the backend narrative endpoint returns and
never embellishes: unavailable results are shown as unavailable.
"""
from __future__ import annotations

from typing import Any, Dict, List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import theme
from ..api_client import GroundStationClient


class PresentationModeView(QWidget):
    SECTION_ORDER = [
        ("question", "QUESTION"),
        ("measurements", "MEASUREMENTS"),
        ("result", "RESULT"),
        ("evidence", "EVIDENCE"),
        ("interpretation", "INTERPRETATION"),
        ("correlations", "CORRELATIONS"),
        ("confidence", "CONFIDENCE"),
        ("limitations", "LIMITATIONS"),
        ("unavailable", "UNAVAILABLE (shown explicitly)"),
        ("disclaimer", "DISCLAIMER"),
    ]

    def __init__(self, client: GroundStationClient, parent=None):
        super().__init__(parent)
        self._client = client
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        title = QLabel("JUDGE / PRESENTATION MODE")
        title.setObjectName("Heading")
        head.addWidget(title)
        head.addStretch(1)
        self.refresh_btn = QPushButton("Refresh narrative")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        root.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._content = QWidget()
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        self._status = QLabel("Press 'Refresh narrative' to load.")
        self._status.setStyleSheet(f"color:{theme.FG_MUTED};")
        root.addWidget(self._status)

    def refresh(self) -> None:
        res = self._client.narrative()
        self._clear()
        if not res.ok or not isinstance(res.data, dict):
            self._status.setText(
                f"NARRATIVE UNAVAILABLE — {res.error or res.state}. "
                f"(Needs the scientific tier running or stored data.)"
            )
            return
        self._render(res.data)
        self._status.setText(
            f"mission={res.data.get('mission_id','?')} "
            f"state={res.data.get('mission_state','?')}"
        )

    def _clear(self) -> None:
        while self._content_lay.count():
            item = self._content_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _render(self, narrative: Dict[str, Any]) -> None:
        for key, heading in self.SECTION_ORDER:
            if key not in narrative:
                continue
            self._add_heading(heading)
            self._add_body(key, narrative[key])

    def _add_heading(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{theme.ACCENT}; font-size:16px; font-weight:bold; margin-top:12px;"
        )
        self._content_lay.addWidget(lbl)

    def _add_body(self, key: str, value: Any) -> None:
        if isinstance(value, str):
            self._add_text(value, emphasis=(key in ("result", "disclaimer")))
        elif isinstance(value, list):
            for entry in value:
                self._add_text(f"•  {entry}")
        elif isinstance(value, dict):
            for k, v in value.items():
                self._add_text(f"•  {k}: {v}")
        else:
            self._add_text(str(value))

    def _add_text(self, text: str, emphasis: bool = False) -> None:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        size = 15 if emphasis else 13
        weight = "bold" if emphasis else "normal"
        lbl.setStyleSheet(f"font-size:{size}px; font-weight:{weight}; margin-left:8px;")
        self._content_lay.addWidget(lbl)
