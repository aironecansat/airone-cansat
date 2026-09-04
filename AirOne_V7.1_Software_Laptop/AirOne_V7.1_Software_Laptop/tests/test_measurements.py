"""Tests for canonical measurement models and fusion invalid-input handling."""
import json
import math
from datetime import datetime, timezone

from src.core.models import (
    DataSource,
    Measurement,
    QualityState,
    TelemetryFrame,
)
from src.data_processing.fusion import FusionEngine


def test_measurement_quality_enum():
    # Every quality state must round-trip through JSON.
    for q in QualityState:
        m = Measurement(
            value=1.0, unit="Pa", timestamp=datetime.now(timezone.utc),
            sensor_id="s", quality=q,
        )
        d = m.to_dict()
        assert d["quality"] == q.value
        assert json.dumps(d)  # serialisable


def test_invalid_measurement_never_zero():
    m = Measurement.invalid("dead_sensor", unit="Pa", field_name="pressure")
    assert m.valid is False
    assert m.quality == QualityState.INVALID
    assert isinstance(m.value, float)
    assert math.isnan(m.value)  # NOT a silent 0.0
    assert m.value != 0.0


def test_fusion_all_invalid_inputs():
    fe = FusionEngine()
    bad1 = Measurement.invalid("bme688", unit="Pa", field_name="pressure")
    bad2 = Measurement.invalid("bmp581", unit="Pa", field_name="pressure")
    fused = fe.fuse_pressure(bad1, bad2)
    assert fused.valid is False
    assert fused.quality == QualityState.INVALID
    assert math.isnan(fused.value)  # no fabricated value


def test_fusion_valid_inputs_weighted():
    ts = datetime.now(timezone.utc)
    m1 = Measurement(1000.0, "Pa", ts, "bme688", uncertainty=2.0, field_name="pressure")
    m2 = Measurement(1010.0, "Pa", ts, "bmp581", uncertainty=1.0, field_name="pressure")
    fused = FusionEngine().fuse_pressure(m1, m2)
    assert fused.valid is True
    assert fused.source == DataSource.FUSED
    # Lower-uncertainty sensor pulls the estimate toward 1010.
    assert 1005.0 < fused.value <= 1010.0


def test_telemetry_frame_valid_filter():
    ts = datetime.now(timezone.utc)
    frame = TelemetryFrame(timestamp=ts)
    frame.add(Measurement(1.0, "Pa", ts, "good", field_name="a"))
    frame.add(Measurement.invalid("bad", field_name="b"))
    assert set(frame.valid_measurements().keys()) == {"a"}
