"""AirOne ground-station GUI package (PyQt5, REST client).

The GUI is a decoupled REST client of the backend API. It degrades
explicitly when PyQt5/matplotlib are unavailable and can never stop or
corrupt the backend workers.
"""
from __future__ import annotations

from .app import gui_available, run_gui

__all__ = ["run_gui", "gui_available"]
