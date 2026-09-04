"""GUI entry point with graceful degradation.

``run_gui`` returns an integer status and NEVER raises into the caller: if
PyQt5 is missing or the display cannot be opened, it logs a clear diagnostic
and returns non-zero so the launcher can continue running headless. A GUI
failure must never take down the backend.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("airone.gui")


def gui_available() -> tuple[bool, str]:
    """Return (available, reason) for the GUI stack."""
    try:
        import PyQt5  # noqa: F401
        from PyQt5 import QtWidgets  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"PyQt5 not importable: {exc}"
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return True, "offscreen"
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "no DISPLAY/WAYLAND_DISPLAY (headless environment)"
    return True, "ok"


def run_gui(
    config: Optional[Dict[str, Any]] = None,
    base_url: str = "http://127.0.0.1:5000",
    username: Optional[str] = None,
    password: Optional[str] = None,
    poll_interval_ms: int = 1000,
) -> int:
    """Launch the Qt GUI. Blocks until the window is closed.

    Returns 0 on clean exit, non-zero if the GUI could not start.
    """
    ok, reason = gui_available()
    if not ok:
        logger.warning("GUI unavailable: %s", reason)
        return 3

    config = config or {}
    gui_cfg = config.get("gui", {}) if isinstance(config, dict) else {}
    api_cfg = config.get("api", {}) if isinstance(config, dict) else {}
    port = int(api_cfg.get("port", 5000))
    host = api_cfg.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    base_url = gui_cfg.get("base_url", base_url or f"http://{host}:{port}")

    # Default to admin so all role-gated views work in the operator console;
    # override via config or environment for a lower-privilege console.
    username = (
        username
        or gui_cfg.get("username")
        or os.environ.get("AIRONE_GUI_USER")
        or "admin"
    )
    # No shipped default password. Without credentials the console starts in
    # an explicit UNAUTHENTICATED state (public /status only) instead of
    # silently trying a well-known password.
    password = (
        password
        or os.environ.get("AIRONE_GUI_PASSWORD")
        or ""
    )
    if not password:
        logger.warning(
            "No GUI password configured (AIRONE_GUI_PASSWORD); console starts "
            "UNAUTHENTICATED — role-gated views stay unavailable."
        )

    try:
        from PyQt5.QtWidgets import QApplication
        from .api_client import GroundStationClient
        from .main_window import MainWindow
        from . import theme
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to import GUI components: %s", exc)
        return 3

    try:
        app = QApplication.instance() or QApplication([])
        app.setApplicationName("AirOne V7.1 Ground Station")
        app.setStyleSheet(theme.STYLESHEET)
        client = GroundStationClient(
            base_url=base_url, username=username, password=password
        )
        # Best-effort initial login so the banner is meaningful immediately.
        login = client.login()
        if not login.ok:
            logger.warning("Initial GUI login failed (%s); GUI will keep retrying.",
                           login.error)
        window = MainWindow(client, poll_interval_ms=poll_interval_ms)
        window.show()
        logger.info("GUI started against %s as %s", base_url, username)
        return int(app.exec_())
    except Exception as exc:  # noqa: BLE001
        logger.exception("GUI crashed during startup: %s", exc)
        return 4
