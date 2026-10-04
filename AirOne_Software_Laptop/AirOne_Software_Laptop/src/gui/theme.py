"""Colour palette and quality/state -> colour maps for the GUI.

Colours encode *honesty*: every quality/connection state has a distinct,
unambiguous colour so an operator can never mistake stale or simulated data
for live valid data.
"""
from __future__ import annotations

# Base dark aerospace palette.
BG = "#0e1116"
BG_PANEL = "#161b22"
BG_RAISED = "#1c2330"
FG = "#e6edf3"
FG_MUTED = "#8b949e"
ACCENT = "#2f81f7"
GRID = "#30363d"

# Quality states (must match core.models.QualityState values).
QUALITY_COLORS = {
    "VALID": "#3fb950",       # green
    "SUSPECT": "#d29922",     # amber
    "INVALID": "#f85149",     # red
    "MISSING": "#6e7681",     # grey
    "STALE": "#a371f7",       # purple
    "CORRUPTED": "#da3633",   # dark red
    "REPAIRED": "#2f81f7",    # blue
    "SIMULATED": "#39c5cf",   # cyan
    "ESTIMATED": "#db6d28",   # orange
}

# Data source colours.
SOURCE_COLORS = {
    "RAW": "#8b949e",
    "CALIBRATED": "#58a6ff",
    "FILTERED": "#2f81f7",
    "FUSED": "#3fb950",
    "ESTIMATED": "#db6d28",
    "SIMULATED": "#39c5cf",
}

# Connection states (match api_client.ConnectionState).
CONNECTION_COLORS = {
    "UNKNOWN": "#6e7681",
    "CONNECTED": "#3fb950",
    "DEGRADED": "#d29922",
    "UNAUTHENTICATED": "#db6d28",
    "DISCONNECTED": "#f85149",
    "UNAVAILABLE": "#da3633",
}

# Mission state colours.
MISSION_COLORS = {
    "BOOT": "#6e7681",
    "SELF_TEST": "#58a6ff",
    "PRELAUNCH": "#d29922",
    "ASCENT": "#3fb950",
    "APOGEE": "#39c5cf",
    "DESCENT": "#db6d28",
    "LANDED": "#a371f7",
    "SAFE": "#2f81f7",
    "FAULT": "#f85149",
}


def quality_color(q: str) -> str:
    return QUALITY_COLORS.get(str(q).upper(), FG_MUTED)


def source_color(s: str) -> str:
    return SOURCE_COLORS.get(str(s).upper(), FG_MUTED)


def connection_color(s: str) -> str:
    return CONNECTION_COLORS.get(str(s).upper(), FG_MUTED)


def mission_color(s: str) -> str:
    return MISSION_COLORS.get(str(s).upper(), FG_MUTED)


STYLESHEET = f"""
QMainWindow, QWidget {{ background: {BG}; color: {FG}; font-size: 13px; }}
QTabWidget::pane {{ border: 1px solid {GRID}; background: {BG}; }}
QTabBar::tab {{
    background: {BG_PANEL}; color: {FG_MUTED};
    padding: 8px 16px; border: 1px solid {GRID}; border-bottom: none;
}}
QTabBar::tab:selected {{ background: {BG_RAISED}; color: {FG}; }}
QGroupBox {{
    border: 1px solid {GRID}; border-radius: 6px; margin-top: 10px;
    background: {BG_PANEL}; font-weight: bold;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {FG_MUTED}; }}
QTableWidget {{ background: {BG_PANEL}; gridline-color: {GRID}; selection-background-color: {ACCENT}; }}
QHeaderView::section {{ background: {BG_RAISED}; color: {FG_MUTED}; border: none; padding: 4px; }}
QPushButton {{
    background: {BG_RAISED}; color: {FG}; border: 1px solid {GRID};
    border-radius: 4px; padding: 6px 12px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {FG_MUTED}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {BG}; color: {FG}; border: 1px solid {GRID}; border-radius: 4px; padding: 4px;
}}
QLabel#Heading {{ font-size: 18px; font-weight: bold; }}
QTextEdit {{ background: {BG_PANEL}; color: {FG}; border: 1px solid {GRID}; }}
"""
