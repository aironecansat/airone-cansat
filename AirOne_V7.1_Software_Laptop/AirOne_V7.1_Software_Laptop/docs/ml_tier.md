# ML / AI Tier

The ML tier (Tier 3) provides **advisory** anomaly detection, distribution
drift monitoring, and a provenance-tracked model registry. It is implemented in
`src/ml/` and surfaced under `/api/v1/ml`.

## The advisory-only guarantee

> The ML/AI tier can inform operators. It can **never** command a mission-state
> change or alter Tier-1/Tier-2 data.

This is enforced structurally, not just by convention:

- `MLAnalysisWorker` holds **no reference** to the mission state machine.
- No file under `src/ml/` imports or references `MissionStateMachine` /
  `mission_machine`. This is asserted by
  `tests/test_ml.py::test_ml_source_does_not_reference_mission_state_machine`.
- Every ML API response carries an advisory note and cannot mutate mission
  state.

## Detectors

| Detector | Dependency | Availability | Method |
|----------|------------|--------------|--------|
| `robust_zscore` | none (pure NumPy) | **Always available** | Median/MAD robust z-score: `|x − median| / (1.4826·MAD)`, cutoff 3.5. Zero-MAD features are guarded, not turned into infinities. |
| `isolation_forest` | scikit-learn | If sklearn imports | Isolation Forest, `contamination='auto'`, 200 estimators. |
| `gmm` | scikit-learn | If sklearn imports | Gaussian Mixture negative log-likelihood, 5th-percentile cutoff. |
| `lstm_forecast` | PyTorch | If torch imports | One-step LSTM forecast; residual z-scored, cutoff 3.0. |
| `ensemble` | — | Uses whichever of the above are available | Rank-normalises each available detector's scores, averages them, and flags a sample when a majority (≥50%) of contributors vote anomalous. Records exactly which detectors contributed. |

`GET /api/v1/ml/detectors` returns a **live** availability probe: for each
detector it reports `available`, the `method`, and — when unavailable — the
exact `reason` (e.g. the import error). Nothing is claimed to work that cannot.

## Feature extraction — no imputation

`extract_feature_matrix` builds the numeric matrix from a window of telemetry
frames. A frame contributes a row **only if every requested feature is present
and valid** in that frame. Incomplete frames are dropped and counted
(`dropped_incomplete`); they are never zero-filled or imputed. The default
feature set is:

```
bme688_temperature, bme688_pressure, bme688_humidity,
gnss_altitude, radiation_cpm, battery_voltage
```

## Drift monitoring

`population_stability_index(expected, actual)` computes the PSI between a
reference distribution and a current window:

| PSI | Classification |
|-----|----------------|
| < 0.10 | STABLE |
| < 0.25 | MODERATE |
| ≥ 0.25 | SIGNIFICANT |
| insufficient data | **UNAVAILABLE** (returns `None`, never a fabricated 0) |

The `DriftMonitor` stores a reference matrix and evaluates per-feature PSI on
demand. Insufficient data yields an explicit unavailable result.

## Training

`POST /api/v1/ml/train` (SCIENTIST role, rate-limited 2/hour) runs
`train_detector` over stored telemetry:

1. Reconstruct frames from persisted measurements.
2. Extract the feature matrix (dropping incomplete frames honestly).
3. Refuse to train if fewer than **20** valid samples — returns `422` with the
   exact count and how many frames were dropped. **No fake model is created.**
4. Fit and score the detector.
5. Persist the artefact via `ModelStore` and record its provenance.

### Model provenance & integrity

Every trained model records:

- `training_data_hash` — a hash of the exact feature matrix it was trained on.
- `sha256_hash` — the SHA-256 of the serialised artefact on disk.

`ModelStore.load(path, expected_sha256)` **recomputes** the file's SHA-256 and
refuses to load if it is missing, if no expected hash is supplied, or if the
hash does not match. A tampered model file therefore fails closed with a
`SafeLoadError` rather than executing an untrusted pickle. This is verified by
`tests/test_ml.py::test_model_store_detects_tampering`.

## Live advisory summary

The `MLAnalysisWorker` maintains a rolling window and periodically scores it
with the ensemble. `GET /api/v1/ml/anomalies` returns the latest summary with
an honest `state`:

| State | Meaning |
|-------|---------|
| `IDLE` | Worker running, no analysis yet. |
| `INSUFFICIENT_DATA` | Fewer than the minimum samples in the window. |
| `UNAVAILABLE` | No detector could run. |
| `AVAILABLE` | A real anomaly summary is present. |
| `NOT_CONFIGURED` (HTTP 503) | The ML worker is not running at all. |

At no point does the tier invent a "clean" result to appear healthy.
