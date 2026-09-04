"""Tests for the 20-stage processing pipeline."""
from datetime import datetime, timezone

from src.core.models import DataSource, Measurement, QualityState, TelemetryFrame
from src.data_processing.pipeline import PipelineContext, ProcessingPipeline


def _frame(**measurements):
    ts = datetime.now(timezone.utc)
    frame = TelemetryFrame(sequence=1, timestamp=ts)
    for name, (value, unit) in measurements.items():
        frame.add(
            Measurement(value=value, unit=unit, timestamp=ts, sensor_id=name,
                        quality=QualityState.VALID, valid=True,
                        source=DataSource.RAW, field_name=name)
        )
    return frame


def test_range_check_rejects_invalid():
    frame = _frame(pressure=(999999.0, "Pa"))
    pipe = ProcessingPipeline(PipelineContext())
    pipe.process(frame)
    m = frame.get("pressure")
    assert m.quality == QualityState.INVALID
    assert m.valid is False


def test_calibration_applies_offset():
    ctx = PipelineContext(calibration_data={"temp_sensor": {"offset": 1.0, "scale": 1.0}})
    ts = datetime.now(timezone.utc)
    frame = TelemetryFrame(sequence=1, timestamp=ts)
    frame.add(Measurement(value=300.0, unit="K", timestamp=ts, sensor_id="temp_sensor",
                          quality=QualityState.VALID, valid=True, field_name="temperature"))
    # Disable filtering/fusion to isolate calibration behaviour.
    pipe = ProcessingPipeline(ctx)
    pipe.process(frame)
    assert abs(frame.get("temperature").value - 301.0) < 1e-9
    assert frame.get("temperature").source == DataSource.CALIBRATED


def test_duplicate_pipeline_stage():
    ctx = PipelineContext()
    pipe = ProcessingPipeline(ctx)
    f1 = _frame(pressure=(101000.0, "Pa"))
    pipe.process(f1)
    f2 = _frame(pressure=(101000.0, "Pa"))
    f2.metadata["duplicate"] = True
    pipe.process(f2)
    assert f2.get("pressure").valid is False
    assert f2.metadata.get("dropped_duplicate") is True


def test_pipeline_continues_after_stage_failure():
    ts = datetime.now(timezone.utc)
    frame = TelemetryFrame(sequence=5, timestamp=ts, crc_valid=False)
    frame.add(Measurement(value=100.0, unit="Pa", timestamp=ts, sensor_id="p",
                          quality=QualityState.VALID, valid=True, field_name="pressure"))
    pipe = ProcessingPipeline(PipelineContext())
    results = pipe.process(frame)
    # All 20 stages ran, none crashed the pipeline.
    assert len(results) == 20
    # CRC-failed frame marks measurements CORRUPTED.
    assert frame.get("pressure").quality == QualityState.CORRUPTED
    assert frame.get("pressure").valid is False


def test_unit_normalize_to_si():
    frame = _frame(pressure=(1013.25, "hPa"))
    pipe = ProcessingPipeline(PipelineContext())
    pipe.process(frame)
    m = frame.get("pressure")
    assert m.unit == "Pa"
    assert abs(m.value - 101325.0) < 1e-6
