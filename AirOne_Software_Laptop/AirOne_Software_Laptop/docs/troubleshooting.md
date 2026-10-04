# Troubleshooting

Symptoms, likely causes, and resolutions. The guiding principle: the system
tells you the truth about its state — read the reported `state`/`quality`
before assuming a bug.

## Startup

**"InsecureConfigError" / refuses to start**
- Cause: `AIRONE_JWT_SECRET` is unset, too short, or a placeholder.
- Fix: `export AIRONE_JWT_SECRET="<>= 32 random chars>"` and restart.

**No telemetry appears (live mode)**
- Cause: no serial device, wrong port, or nothing transmitting.
- Diagnosis: check the receiver's reported port error in the logs. The system
  will **not** fabricate telemetry.
- Fix: set `telemetry.serial_port`, verify wiring/baud, or run `--simulate`.

**GUI does not open**
- Cause: no `DISPLAY` (headless) or PyQt5 not installed.
- Behaviour: the launcher reports this and continues headless — this is
  expected, not a crash.
- Fix: install PyQt5 and run on a machine with a display, or use the API.

## Data quality

**Values shown red/orange in the GUI**
- These are `INVALID` / `STALE` / `SUSPECT` measurements. The colour is the
  system telling you not to trust the number. Investigate the sensor, not the
  GUI.

**A measurement is `CORRUPTED`**
- The frame failed CRC (and FEC could not recover it, or `reedsolo` is not
  installed). Check RF link quality; install `reedsolo` to enable FEC recovery.

**An analysis says "insufficient data"**
- Expected behaviour when too few valid samples exist. Let more telemetry
  accumulate; the analysis will compute once it has enough.

## ML tier

**`/ml/anomalies` returns 503 NOT_CONFIGURED**
- The ML worker is not running. Ensure the orchestrator started it (it is
  wired automatically at startup). This is honest reporting, not an error to
  paper over.

**A detector shows `available: false`**
- Its optional dependency is missing. `isolation_forest`/`gmm` need
  scikit-learn; `lstm_forecast` needs PyTorch. `robust_zscore` always works.
- Fix: `pip install scikit-learn` and/or the CPU build of `torch`.

**Training returns 422**
- Either there is no stored telemetry yet, or fewer than 20 valid samples after
  dropping incomplete frames. The response states the exact counts. Accumulate
  more data and retry.

**`SafeLoadError` when loading a model**
- The artefact's SHA-256 does not match (tampered/corrupted) or no expected
  hash was provided. Retrain to produce a fresh, verified artefact.

## API

**401 Unauthorized**
- Missing/expired token. Log in again or refresh.

**403 Forbidden**
- Your role is too low for that endpoint. See [security_model.md](security_model.md).

**429 Too Many Requests**
- Rate limit hit. Back off; adjust `api.rate_limits` if the limit is too tight
  for your deployment.

## Tests

**GUI smoke tests fail with a Qt platform error**
- Run with `QT_QPA_PLATFORM=offscreen`. On CI, also install
  `libgl1 libegl1 libxkbcommon0 libdbus-1-3`.

**Security tests fail on import**
- Ensure `AIRONE_JWT_SECRET` is set in the environment before running pytest
  (the shared `conftest.py` sets a test default automatically).
