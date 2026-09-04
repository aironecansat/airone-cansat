# Failure Modes & Honest Degradation

AirOne V7.1 is built around one principle: **never present fabricated, stale,
or fake-complete data as real.** This document defines the vocabulary the
system uses to tell the truth about its own state, and how each subsystem
degrades.

## Quality vocabulary (`QualityState`)

Every `Measurement` carries a `QualityState`:

| State | Meaning |
|-------|---------|
| `VALID` | Trustworthy, in-range, current. |
| `SUSPECT` | Passed basic checks but a rate/consistency check flagged it — treat with caution. |
| `INVALID` | Failed a hard check (range, schema). The numeric value must not be used. |
| `MISSING` | The field was expected but absent. |
| `STALE` | Older than the freshness bound; no longer current. |
| `CORRUPTED` | Failed CRC or FEC could not recover it. |
| `REPAIRED` | Recovered via FEC; usable but flagged as reconstructed. |
| `SIMULATED` | Produced by the simulator, not a real sensor. |
| `ESTIMATED` | Derived/inferred rather than directly measured. |

An `INVALID`/`MISSING`/`CORRUPTED` measurement is forced to `valid = False` by
the model itself — the flag and the state can never disagree.

## Provenance vocabulary (`DataSource`)

`RAW`, `CALIBRATED`, `FILTERED`, `FUSED`, `ESTIMATED`, `SIMULATED`. Every value
knows how it was produced, so a fused or estimated number is never mistaken for
a raw sensor reading.

## Tier-level degradation

| Tier | Failure | Observable result |
|------|---------|-------------------|
| 1 Raw | Serial port missing | Receiver reports the port error; the system does not invent telemetry. Use `--simulate` for demos. |
| 1 Raw | CRC failure | Frame marked `CORRUPTED`; not analysed. |
| 2 Science | Too few samples | Analysis returns an explicit insufficient/unavailable result, not a guess. |
| 2 Science | One analysis throws | That analysis is marked failed; the other 36 and all of Tier 1 continue. |
| 3 ML | sklearn/torch absent | Those detectors report `available=false` with the import error; `robust_zscore` still works. |
| 3 ML | Worker not running | `/ml/anomalies` returns `503 NOT_CONFIGURED`. |
| 3 ML | Model file tampered | `ModelStore.load` raises `SafeLoadError`; the model is not loaded. |
| 4 GUI | No display / PyQt5 absent | Launcher reports it and runs headless; backend unaffected. |
| 4 GUI | API unreachable | Views show the error and the tier state; no cached/fake numbers are shown as live. |

## API-level honesty

- Endpoints that depend on a live worker return `503` with
  `data.state = "NOT_CONFIGURED"` when that worker is absent.
- Requests that are well-formed but cannot be honestly fulfilled (e.g. training
  with no data) return `422` with a precise reason — never a fabricated success.
- The response envelope always includes `success`, `error`, and often a
  `state`, so a client can always distinguish "no data" from "data is zero".

## Secrets & startup

If `AIRONE_JWT_SECRET` is unset or is a known placeholder, the system refuses
to start (`InsecureConfigError`). This is a fail-closed control: it is better to
not start than to run with a guessable secret.

## The anti-pattern this system avoids

The following are explicitly **never** done anywhere in the codebase:

- Returning `0.0` (or any placeholder) for a failed sensor instead of `INVALID`.
- Imputing missing features with fabricated values before ML/analysis.
- Showing the last-known value as if it were live when it is actually `STALE`.
- Reporting a PSI of 0 when there is not enough data to compute one.
- Claiming a detector/model works when its dependency is missing.
