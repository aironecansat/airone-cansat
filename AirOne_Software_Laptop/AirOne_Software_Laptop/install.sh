#!/usr/bin/env bash
# AirOne Ground Station installer — Linux / macOS
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[AirOne] Installing dependencies from requirements_run.txt..."
pip install -r "$SCRIPT_DIR/requirements_run.txt"

# Linux: add udev rules for CP210x and CH340 so non-root can use the port
if [[ "$(uname -s)" == "Linux" ]]; then
    RULES_FILE="/etc/udev/rules.d/99-airone-serial.rules"
    echo "[AirOne] Installing udev rules to $RULES_FILE (needs sudo)..."
    sudo tee "$RULES_FILE" > /dev/null << 'UDEV'
# AirOne CanSat USB serial adapters
# SiLabs CP210x
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", MODE="0666", GROUP="dialout"
# WCH CH340
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0666", GROUP="dialout"
# FTDI
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", MODE="0666", GROUP="dialout"
UDEV
    sudo udevadm control --reload-rules && sudo udevadm trigger || true
    echo "[AirOne] Adding current user to dialout group..."
    sudo usermod -aG dialout "$USER" 2>/dev/null || true
    echo "[AirOne] NOTE: log out and back in for group change to take effect."
fi

# Create desktop shortcut (Linux)
if [[ "$(uname -s)" == "Linux" ]] && [[ -d "$HOME/Desktop" ]]; then
    DESKTOP="$HOME/Desktop/AirOne Ground Station.desktop"
    cat > "$DESKTOP" << DESK
[Desktop Entry]
Version=1.0
Type=Application
Name=AirOne Ground Station
Comment=AirOne CanSat telemetry dashboard
Exec=bash -c "cd '$SCRIPT_DIR' && python run.py"
Icon=network-wireless
Terminal=true
Categories=Science;
DESK
    chmod +x "$DESKTOP" 2>/dev/null || true
    echo "[AirOne] Desktop shortcut created: $DESKTOP"
fi

echo ""
echo "[AirOne] Install complete. Run with:  python run.py"
