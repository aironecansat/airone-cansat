# Operator Manual

This manual covers running AirOne during a mission or a demonstration.

## 1. Starting the ground station

All commands are run from the `airone/` directory.

### 1.1 Prerequisites

```bash
pip install -r requirements.txt
export AIRONE_JWT_SECRET="<a long random secret, >= 32 chars>"
```

The `AIRONE_JWT_SECRET` environment variable is **mandatory**. The system
refuses to start with a default/placeholder secret — this is a deliberate
security control, not a bug (see [security_model.md](security_model.md)).

### 1.2 Launch modes

| Command | What it does |
|---------|--------------|
| `python launcher.py` | Starts the backend (API + workers). Reads live telemetry from the configured serial port. |
| `python launcher.py --simulate` | Feeds a physics-based simulated mission through the **real** pipeline. All resulting data is tagged `SIMULATED`. |
| `python launcher.py --simulate --sim-loop` | Continuously replays the simulated mission (useful for demos). |
| `python launcher.py --gui` | Also starts the PyQt5 operator GUI (if a display and PyQt5 are available). |
| `python launcher.py --validate-only` | Validates configuration and environment, then exits without starting. |

Simulation tuning flags: `--sim-seed <int>`, `--sim-apogee <metres>`,
`--sim-rate <Hz>`.

> **Honesty note:** Data produced in `--simulate` mode is never presented as
> real. Every measurement carries `source = SIMULATED` and the GUI shows a
> simulation banner. This is how you demo the system without ever faking a
> real reading.

### 1.3 Accounts

No accounts ship with the software. Create them with the launcher (the
password comes from `AIRONE_ADMIN_PASSWORD` or is generated and printed once):

```bash
AIRONE_ADMIN_PASSWORD='<strong password>' python launcher.py --create-admin
python launcher.py --create-user ops1 --role OPERATOR --generate-password
python launcher.py --reset-password ops1 --must-change
python launcher.py --list-users
```

Passwords need 12+ characters with upper/lower/digit/symbol. Five wrong
attempts lock the account for 5 minutes (doubling on repeat); the API answers
`429 ACCOUNT_LOCKED` with `Retry-After`. A user flagged *must change* gets
`403 PASSWORD_CHANGE_REQUIRED` until `POST /api/v1/auth/change-password`.

The GUI logs in with `AIRONE_GUI_USER` / `AIRONE_GUI_PASSWORD`; without them
it runs `UNAUTHENTICATED` and shows only public status.

`python launcher.py --verify-audit` checks the audit log hash chain (exit 0 =
intact, 2 = broken, reports the first bad line).

## 2. The GUI

The GUI is a **decoupled REST client** of the backend API. It runs in the main
thread; the backend runs in worker threads. A GUI crash therefore cannot
corrupt telemetry capture — the two are isolated by design. In a headless
environment (`--gui` with no `DISPLAY`), the launcher reports this honestly and
continues running the backend without a window.

### 2.1 Tabs

1. **Mission Control** — live mission state, latest telemetry, mission-state controls.
2. **Science Dashboard** — the 37 scientific analyses with values, units, and uncertainty.
3. **AI Diagnostics** — advisory ML anomaly detection, drift, and the model registry.
4. **Map** — GNSS ground track (requires valid GNSS fixes).
5. **Replay** — step through recorded telemetry history.
6. **Presentation / Judge Mode** — a clean, large-format read-only view for demos.
7. **Configuration** — view/edit runtime configuration (ENGINEER role required).

### 2.2 Colour vocabulary

The GUI colours every value by its quality state. A red/orange cell is not a
styling choice — it means the data is `INVALID`, `STALE`, or `SUSPECT` and must
not be trusted. Never read a number without reading its colour.

## 3. Judge / Presentation mode

Presentation mode is designed for scored demonstrations:

- Large fonts, high-contrast theme, no editable controls.
- Shows only data the system can currently stand behind. If a value is stale or
  a tier is down, the panel says so rather than showing an old number.
- A persistent banner shows whether the session is `LIVE` or `SIMULATED`.

## 4. Exporting data

Telemetry can be exported via the API (`GET /api/v1/telemetry/export`,
OPERATOR role, rate-limited to 5/minute). Exports include the quality state of
every measurement so downstream consumers inherit the same honesty guarantees.

## 5. Shutting down

`Ctrl-C` triggers a graceful shutdown: workers are stopped in reverse
dependency order, in-flight telemetry is flushed to the database, and a final
shutdown-state snapshot is written to `data/last_shutdown_state.json`.
