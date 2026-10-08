# AirOne Ground Station — Quick Start

## Requirements
- Python 3.9+
- A CP210x, CH340, or FTDI USB-to-serial bridge connected to the CanSat ground receiver

## 1. Install dependencies (one time)

```bash
pip install -r requirements_run.txt
```

Or use the platform installer:
```bash
bash install.sh      # Linux / macOS
install.bat          # Windows (run as Administrator)
```

## 2. Plug in the USB ground bridge

The ground station auto-detects CP210x (SiLabs), CH340, and FTDI adapters.
No port configuration required.

## 3. Launch

```bash
python run.py
```

The dashboard opens automatically at **http://localhost:5000**

### Options

| Flag | Description |
|------|-------------|
| `--port /dev/ttyUSB0` | Override auto-detected port |
| `--port COM5` | Windows explicit port |
| `--baud 115200` | Override baud rate (default 115200) |
| `--flask-port 5000` | Dashboard port (default 5000) |
| `--no-browser` | Don't open browser automatically |

## Simulation / demo mode

Demo mode is **locked** until 10 real flights have been logged to the local database
(`data/flights.db`). After 10 completed flights it unlocks automatically.

## Data storage

All telemetry is stored in `data/flights.db` (SQLite).
A runtime JWT secret is auto-generated to `data/.runtime_secrets.json` — this file
is gitignored and must not be committed.
