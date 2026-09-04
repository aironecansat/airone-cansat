# AirOne V7.1 — PACKAGE 2 of 2 — GROUND-STATION SOFTWARE (runs on the laptop)

This package contains the ground station + AI analysis platform. The CanSat flight firmware is in PACKAGE 1 (AirOne_V7.1_Firmware_CanSat.zip).

---

# AirOne V7.1 — CanSat Ground Station & AI Scientific Analysis Platform

**Authored by Team AirOne.** &nbsp;|&nbsp; See [`AUTHORS`](AUTHORS) and [`LICENSE`](LICENSE).

A complete, runnable aerospace ground station. It receives binary telemetry,
validates and processes it through an explicit 20-stage pipeline, fuses
redundant sensors, runs 37 deterministic scientific analyses, provides an
**advisory-only** ML anomaly-detection tier, enforces real security
(JWT + bcrypt + RBAC + a tamper-evident audit chain), persists everything to
SQLite, and exposes it all through a Flask REST API and an optional PyQt5 GUI.

## The principle behind every design decision

> **Never present fabricated, stale, or fake-complete data as real.**

Every value carries **quality, provenance, and uncertainty**. A failed sensor
never returns a silent `0.0` — it returns a `Measurement` with `valid=False`
and `quality=INVALID`. Every subsystem either works completely, is explicitly
disabled with a visible reason, or is unavailable because of a genuinely
missing dependency reported with a clear diagnostic. See
[`docs/failure_modes.md`](docs/failure_modes.md).

## Capability tiers (higher tiers never corrupt lower ones)

1. **Tier 1 — Raw telemetry:** receive, parse, CRC, persist. The floor.
2. **Tier 2 — Science:** 37 analyses with propagated uncertainty.
3. **Tier 3 — ML/AI:** advisory anomaly detection & drift. **Cannot change
   mission state.**
4. **Tier 4 — Presentation:** GUI / judge mode, a pure REST client.

## Features

- **Binary telemetry protocol** — `MAGIC+VERSION+TYPE+SEQ+TS+LEN+FLAGS+PAYLOAD+CRC32`
  (little-endian), resync-safe streaming parser, CRC validation,
  duplicate/out-of-order flagging, optional Reed–Solomon FEC.
- **20-stage processing pipeline** — each stage explicit; a stage failure marks
  the affected measurement and continues rather than crashing the frame.
- **Filtering & fusion** — Kalman/EKF/moving-average filters; multi-sensor
  fusion with uncertainty propagation; provenance tagged (`RAW`/`FILTERED`/`FUSED`).
- **37 scientific analyses** across 9 categories (atmospheric, descent, gases,
  GNSS, magnetic, power, radiation, UV/solar, composite) — correlation is never
  reported as causation.
- **Advisory ML tier** — robust median/MAD detector (pure NumPy, always
  available) + optional Isolation Forest, GMM, and PyTorch LSTM detectors, in an
  ensemble; PSI drift monitoring; SHA-256-checksummed model registry.
- **Physics-based simulator** — ISA atmosphere, sensor models, flight profile;
  all output labelled `SIMULATED`. Feeds the *real* pipeline.
- **Security** — env-var JWT secret with startup enforcement, bcrypt passwords,
  hierarchical RBAC, hash-chained tamper-evident audit log, per-endpoint rate
  limits.
- **Storage** — SQLite (WAL), numbered migrations, typed repositories.
- **REST API** — consistent JSON envelope, RBAC decorators, marshmallow
  validation, honest `NOT_CONFIGURED`/`UNAVAILABLE` degradation.
- **PyQt5 GUI** — 7 tabs incl. judge/presentation mode; a decoupled REST client
  that degrades honestly and cannot corrupt the backend.

## Installation

```bash
cd airone
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_v71.txt
```

Optional extras degrade explicitly if absent:
- `scikit-learn` → Isolation Forest + GMM detectors
- `torch` (CPU) → LSTM forecast detector
- `reedsolo` → FEC recovery
- `PyQt5` + `matplotlib` → GUI

## Configuration: the JWT secret (required)

The ground station refuses to start with a placeholder secret. Set a strong one
(≥ 32 chars):

```bash
export AIRONE_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

## Quickstart

```bash
# Validate environment without starting workers
python launcher.py --validate-only

# Start the full ground station (API on :5000), receiving a real CanSat
# over a LoRa E22 on USB-serial (see docs/hardware_connection.md)
python launcher.py --serial-port /dev/ttyUSB0 --baud 115200 --force-baud

# Or auto-discover the radio:
python launcher.py --config config/default_config.yaml

# No hardware? Demo through the identical real pipeline (isolated simulation)
python launcher.py --simulate --sim-loop

# Also start the operator GUI (needs a display + PyQt5)
python launcher.py --simulate --gui
```

No accounts ship with the software — create the first administrator once:

```bash
AIRONE_ADMIN_PASSWORD='<strong password>' python launcher.py --create-admin
# or print a single-use generated password once:
python launcher.py --create-admin --generate-password
```

Then:

```bash
curl http://localhost:5000/api/v1/status
curl -X POST http://localhost:5000/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"<the password you set>"}'
```

The API binds to `127.0.0.1` by default (plain HTTP, no TLS). Copy
`.env.example` to `.env` for the full list of environment variables.

> Inside the Abacus AI Agent VM, `localhost` refers to the VM, not your machine.

## Running the tests

```bash
cd airone
AIRONE_JWT_SECRET=unit_test_secret_key_that_is_definitely_long_enough_123456 \
  QT_QPA_PLATFORM=offscreen python -m pytest -q
```

**154 tests** should pass (optional-dependency tests self-skip if a dep is
absent). See [`docs/test_plan.md`](docs/test_plan.md).

## Documentation

Full documentation is in [`docs/`](docs/): operator & engineering manuals, API
reference, telemetry protocol, scientific methods, ML tier, failure modes,
security model, configuration reference, troubleshooting, and the test plan.

## Project layout

```
airone/
├── src/
│   ├── core/            # models, events, errors, mission state machine
│   ├── telemetry/       # protocol, parser, fec, timesync
│   ├── data_processing/ # 20-stage pipeline, filters, fusion
│   ├── scientific/      # 37 analyses across 9 categories + registry
│   ├── simulation/      # ISA atmosphere, sensor models, flight profile
│   ├── ml/              # detectors, features, drift, registry, trainer
│   ├── security/        # config, auth, users, passwords, lockout, rbac, audit, rate_limit
│   ├── storage/         # database (WAL), migrations, repositories
│   ├── api/             # Flask app, schemas, middleware, routes
│   ├── communication/   # serial transport (E22/USB auto-discovery, reconnect)
│   ├── workers/         # receiver, processor, persistence, scientific, ml, health, orchestrator
│   └── system_logging.py
├── firmware/            # ESP32 CanSat flight firmware (LoRa E22, AirOne frames)
│   └── airone_cansat/   # .ino sketch + airone_frame.h (CRC32 + optional HMAC tag)
├── gui/  (src/gui)      # PyQt5 REST-client GUI (7 tabs incl. judge mode)
├── tests/               # 150+ tests
├── docs/                # full documentation set (incl. hardware_connection.md)
├── config/default_config.yaml
├── migrations/          # 001_initial.sql, 002_users_and_tokens.sql
├── launcher.py
└── requirements_v71.txt
```

## Security defaults

- **No default accounts.** Bootstrap with `launcher.py --create-admin`; demo
  accounts exist only behind `AIRONE_ALLOW_DEFAULT_USERS=1` and must change
  their password on first login.
- Passwords: bcrypt hashes only, 12+ character policy, per-account lockout with
  exponential back-off and per-IP caps.
- JWT: HS256 pinned, `iss`/`aud`/`type`/`jti` required, refresh rotation,
  revocation persisted across restarts.
- API: loopback bind by default, strict schemas (unknown fields rejected),
  restricted CORS, hardened response headers, body size cap, runtime config
  limited to non-security keys.
- Telemetry link: optional HMAC-SHA256 frame tag (`AIRONE_LINK_KEY`,
  firmware `AIRONE_LINK_KEY_HEX`) with explicit `AUTHENTICATED` /
  `UNAUTHENTICATED` / `UNVERIFIABLE` / `INVALID_TAG` states; header pre-checks
  and hostile-payload limits.
- Audit log: hash chain across restarts, `launcher.py --verify-audit`.
- Dependencies: `pip-audit` clean as of 2026-09-04.

Full threat model, controls, bootstrap and **residual risks** (no TLS, no
radio confidentiality) in [`docs/security_model.md`](docs/security_model.md).
