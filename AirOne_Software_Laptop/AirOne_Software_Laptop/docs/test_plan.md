# Test Plan

The suite has **154 tests** across 16 files. They are the executable evidence
that the honesty and capability-tier guarantees in this documentation actually
hold.

## Running

```bash
cd airone
AIRONE_JWT_SECRET=test_secret_key_that_is_definitely_long_enough_123456 \
  QT_QPA_PLATFORM=offscreen python -m pytest -q
```

CI runs the same suite on Python 3.10/3.11/3.12 (see `.github/workflows/ci.yml`).

## Coverage by file

| File | Tests | What it proves |
|------|-------|----------------|
| `test_protocol.py` | 7 | Frame build/parse round-trips, CRC detection, resync, duplicate/out-of-order flagging. |
| `test_measurements.py` | 5 | `Measurement`/`QualityState` coherence — invalid data can never masquerade as valid. |
| `test_pipeline.py` | 5 | The 20-stage pipeline tags quality correctly and isolates per-stage failures. |
| `test_mission_state_machine.py` | 5 | Only legal mission-state transitions are allowed. |
| `test_security.py` | 7 | bcrypt hashing, JWT create/decode/expiry, RBAC gating, audit hash chain, secret enforcement. |
| `test_scientific.py` | 10 | Representative analyses compute correct values with propagated uncertainty and honest insufficiency. |
| `test_scientific_worker_integration.py` | 2 | End-to-end Tier-1→Tier-2: simulated packets → real pipeline → analyses → persistence. |
| `test_simulation.py` | 7 | ISA atmosphere, sensor models, and flight profile behave physically; outputs are labelled `SIMULATED`. |
| `test_ml.py` | 17 | Detector correctness, no-imputation feature extraction, checksum tamper refusal, honest PSI drift, explicit training failures, and the advisory-only invariant. |
| `test_api.py` | 11 | Response envelope, 401/403/404/422/503 failure modes, and `NOT_CONFIGURED` honesty at the HTTP boundary. |
| `test_gui_smoke.py` | 4 | The GUI builds all tabs headlessly and degrades honestly when the API is unreachable. |

## Key invariants asserted

- **Advisory-only ML:** `test_ml.py` asserts the ML worker holds no
  mission-machine handle and that no `src/ml/` file references the mission state
  machine at all.
- **No imputation:** feature extraction drops incomplete/invalid frames and
  counts them; it never fabricates a value.
- **Integrity:** a tampered model artefact fails to load (`SafeLoadError`).
- **Honest drift:** PSI returns `None` (→ `UNAVAILABLE`) on insufficient data,
  never a fabricated 0; identical distributions classify `STABLE`, shifted
  ones `SIGNIFICANT`.
- **Honest API:** training with no data returns `422` with a precise reason;
  a missing worker returns `503 NOT_CONFIGURED` — never a fake success.

## Security hardening tests

- `test_security_hardening.py` — password policy (length, classes, deny-list,
  username), `UserStore` CRUD/persistence/dev-account gate, `LoginThrottle`
  back-off and per-IP lock, JWT `iss`/`aud`/`type`/`jti`/algorithm pinning
  (`none`, HS512 rejected), lifetime caps, persistent revocation and pruning.
- `test_api_security.py` — unknown-field rejection, body cap (413), CORS
  wildcard refusal, security headers, `X-Request-ID` sanitisation, generic
  login errors, lockout (429 + `Retry-After`), disabled accounts, refresh
  rotation and reuse detection, must-change flow, admin user management,
  immutable/redacted config, honest `/status`.
- `test_telemetry_hardening.py` — header pre-check counters, replay flag,
  HMAC accept/reject/`UNVERIFIABLE`/required, hostile JSON payloads (size,
  depth, type, provenance upgrade, timestamps), `AuthenticateStage` verdicts,
  authenticated simulator frames, loopback bind resolution, audit-chain
  verification (tamper, deletion, restart, CLI), launcher bootstrap CLI.
- `test_firmware_frame_parity.py` — C/Python frame parity including the
  authenticated frame, SHA-256 and RFC 4231 HMAC vectors.

## Expected result

```
154 passed
```

Any failure should be treated as a real regression, not flaked away.
