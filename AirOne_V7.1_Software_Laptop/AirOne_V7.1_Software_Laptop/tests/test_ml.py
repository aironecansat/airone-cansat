"""Phase 4 ML tier tests.

Covers the honesty-critical invariants:
  * the always-available robust detector deterministically flags an injected
    outlier;
  * the ensemble records which detectors actually contributed;
  * feature extraction never imputes — incomplete frames are dropped/counted;
  * model artefacts are checksum-verified on load (tampering is refused);
  * drift PSI is honest (identical -> STABLE, shifted -> SIGNIFICANT,
    insufficient data -> None, never a fabricated zero);
  * training fails explicitly on insufficient samples;
  * the ML tier is advisory only and holds NO reference to the mission machine.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from src.core.models import Measurement, TelemetryFrame
from src.ml import available_detectors
from src.ml.detectors import EnsembleDetector, RobustZScoreDetector
from src.ml.drift import (
    DriftMonitor,
    classify_psi,
    population_stability_index,
)
from src.ml.features import DEFAULT_FEATURES, extract_feature_matrix
from src.ml.registry import ModelStore, SafeLoadError
from src.ml.trainer import train_detector

np = pytest.importorskip("numpy")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _frame(seq, values, drop=None):
    f = TelemetryFrame(sequence=seq, timestamp=datetime.now(timezone.utc))
    for name, v in values.items():
        if drop and name in drop:
            continue
        f.add(Measurement(value=v, unit="u", timestamp=datetime.now(timezone.utc),
                          sensor_id=name, field_name=name))
    return f


def _normal_frames(n, seed=1):
    rng = np.random.default_rng(seed)
    frames = []
    for i in range(n):
        frames.append(_frame(i, {f: float(rng.normal(20, 1)) for f in DEFAULT_FEATURES}))
    return frames


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------
def test_robust_zscore_always_available():
    avail = available_detectors()
    assert avail["robust_zscore"]["available"] is True


def test_robust_zscore_flags_injected_outlier():
    rng = np.random.default_rng(0)
    rows = [list(r) for r in rng.normal(0, 1, (100, 3))]
    rows[50] = [12.0, 12.0, 12.0]  # unmistakable outlier
    det = RobustZScoreDetector()
    det.fit(rows)
    res = det.score(rows)
    assert res.available is True
    assert res.is_anomaly[50] is True
    # It should be the strongest anomaly.
    assert res.scores[50] == max(res.scores)


def test_ensemble_reports_contributors():
    rows = [list(r) for r in np.random.default_rng(2).normal(0, 1, (80, 3))]
    ens = EnsembleDetector()
    ens.fit(rows)
    res = ens.score(rows)
    assert res.available is True
    # robust_zscore is always present; at least one contributor guaranteed.
    assert "robust_zscore" in res.contributors
    assert len(res.contributors) >= 1


def test_detector_result_sanitises_non_finite():
    det = RobustZScoreDetector()
    rows = [[0.0, 0.0], [0.0, 0.0], [1.0, 1.0]]  # zero-MAD feature guarded
    det.fit(rows)
    d = det.score(rows).to_dict()
    for s in d["scores"]:
        assert s is None or math.isfinite(s)


# --------------------------------------------------------------------------
# feature extraction — no imputation
# --------------------------------------------------------------------------
def test_feature_extraction_drops_incomplete_frames():
    frames = _normal_frames(10)
    # Corrupt one frame by removing a required feature.
    frames.append(_frame(99, {f: 20.0 for f in DEFAULT_FEATURES},
                         drop={DEFAULT_FEATURES[0]}))
    fm = extract_feature_matrix(frames, DEFAULT_FEATURES)
    assert fm.n_samples == 10          # the incomplete frame was NOT imputed
    assert fm.dropped_incomplete == 1
    assert fm.total_frames == 11


def test_feature_extraction_rejects_invalid_measurement():
    f = TelemetryFrame(sequence=1, timestamp=datetime.now(timezone.utc))
    # An explicitly invalid measurement must never become a feature value.
    f.add(Measurement.invalid(sensor_id=DEFAULT_FEATURES[0],
                              field_name=DEFAULT_FEATURES[0]))
    for name in DEFAULT_FEATURES[1:]:
        f.add(Measurement(value=1.0, unit="u", timestamp=datetime.now(timezone.utc),
                          sensor_id=name, field_name=name))
    fm = extract_feature_matrix([f], DEFAULT_FEATURES)
    assert fm.n_samples == 0
    assert fm.dropped_incomplete == 1


# --------------------------------------------------------------------------
# model store — checksum integrity
# --------------------------------------------------------------------------
def test_model_store_detects_tampering(tmp_path):
    store = ModelStore(directory=str(tmp_path))
    if not store.available:
        pytest.skip("joblib unavailable")
    det = RobustZScoreDetector()
    det.fit([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
    path, sha = store.save(det, "robust_zscore", "v1")
    # Loads cleanly with the correct hash.
    loaded = store.load(path, sha)
    assert loaded is not None
    # Tamper with the artefact bytes.
    with open(path, "ab") as fh:
        fh.write(b"TAMPER")
    with pytest.raises(SafeLoadError):
        store.load(path, sha)


def test_model_store_refuses_load_without_expected_hash(tmp_path):
    store = ModelStore(directory=str(tmp_path))
    if not store.available:
        pytest.skip("joblib unavailable")
    det = RobustZScoreDetector()
    det.fit([[1.0], [2.0], [3.0]])
    path, _sha = store.save(det, "robust_zscore", "v1")
    with pytest.raises(SafeLoadError):
        store.load(path, expected_sha256="")


# --------------------------------------------------------------------------
# drift — honest PSI
# --------------------------------------------------------------------------
def test_psi_identical_is_stable():
    rng = np.random.default_rng(5)
    a = rng.normal(0, 1, 600)
    b = rng.normal(0, 1, 600)
    psi = population_stability_index(a, b)
    assert psi is not None
    assert classify_psi(psi) == "STABLE"


def test_psi_shifted_is_significant():
    rng = np.random.default_rng(6)
    a = rng.normal(0, 1, 600)
    c = rng.normal(4, 1, 600)
    psi = population_stability_index(a, c)
    assert psi is not None
    assert classify_psi(psi) == "SIGNIFICANT"


def test_psi_insufficient_returns_none_not_zero():
    assert population_stability_index([1, 2], [1, 2]) is None
    assert classify_psi(None) == "UNAVAILABLE"


def test_drift_monitor_reference_lifecycle():
    dm = DriftMonitor()
    assert dm.has_reference() is False
    frames = _normal_frames(40)
    fm = extract_feature_matrix(frames, DEFAULT_FEATURES)
    dm.set_reference(fm.rows, fm.feature_names)
    assert dm.has_reference() is True
    results = dm.evaluate(fm.rows, fm.feature_names)
    assert len(results) == len(fm.feature_names)


# --------------------------------------------------------------------------
# trainer
# --------------------------------------------------------------------------
def test_train_detector_succeeds_with_enough_data(tmp_path):
    store = ModelStore(directory=str(tmp_path))
    frames = _normal_frames(60)
    out = train_detector(frames, model_type="robust_zscore", store=store)
    assert out.ok is True
    assert out.n_samples == 60
    assert out.training_data_hash  # provenance recorded
    if store.available:
        assert out.sha256_hash


def test_train_detector_insufficient_samples_fails_explicitly():
    frames = _normal_frames(5)
    out = train_detector(frames, model_type="robust_zscore")
    assert out.ok is False
    assert "insufficient" in out.reason.lower()


def test_train_detector_unknown_type_fails():
    frames = _normal_frames(30)
    out = train_detector(frames, model_type="does_not_exist")
    assert out.ok is False
    assert "unknown" in out.reason.lower()


# --------------------------------------------------------------------------
# spec invariant: ML is advisory-only, never touches mission state
# --------------------------------------------------------------------------
def test_ml_worker_has_no_mission_machine_reference():
    import queue

    from src.workers.ml_worker import MLAnalysisWorker

    worker = MLAnalysisWorker(queue.Queue())
    # The worker must NOT hold any handle that could mutate mission state.
    for attr in ("mission_machine", "mission_state_machine", "_mission_machine"):
        assert not hasattr(worker, attr)
    summary = worker.latest_summary()
    # Advisory note must be present so operators never treat ML as authoritative.
    assert "advisory" in str(summary).lower() or summary.get("state") in {
        "IDLE", "INSUFFICIENT_DATA", "UNAVAILABLE", "AVAILABLE",
    }


def test_ml_source_does_not_reference_mission_state_machine():
    """No file in src/ml/ may import or reference the mission state machine."""
    import os

    ml_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "src", "ml")
    offenders = []
    for root, _dirs, files in os.walk(ml_dir):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(root, fn), "r", encoding="utf-8") as fh:
                text = fh.read()
            if "MissionStateMachine" in text or "mission_machine" in text:
                offenders.append(fn)
    assert offenders == [], f"ML tier must not touch mission state: {offenders}"
