"""20-stage telemetry processing pipeline.

Each stage is an explicit :class:`PipelineStage`. A failure inside a stage
never crashes the pipeline: the affected measurements are marked with an
appropriate :class:`QualityState` and processing continues. Every stage returns
a :class:`PipelineStageResult` recording success, timing, and error text.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Callable, Dict, List, Optional

from ..core.errors import CRCError, FrameError
from ..core.events import EventBus, TOPIC_VISUALIZATION, get_event_bus
from ..core.models import (
    DataSource,
    Measurement,
    MissionState,
    PipelineStageResult,
    QualityState,
    TelemetryFrame,
    utcnow,
)
from ..telemetry import fec as fec_mod
from ..telemetry import protocol
from .filters import FilterRegistry
from .fusion import FusionEngine

logger = logging.getLogger(__name__)


# SI unit conversion factors (multiply raw -> SI).
_UNIT_TO_SI: Dict[str, tuple] = {
    "hPa": (100.0, 0.0, "Pa"),
    "mbar": (100.0, 0.0, "Pa"),
    "kPa": (1000.0, 0.0, "Pa"),
    "Pa": (1.0, 0.0, "Pa"),
    "C": (1.0, 273.15, "K"),
    "degC": (1.0, 273.15, "K"),
    "K": (1.0, 0.0, "K"),
    "km": (1000.0, 0.0, "m"),
    "m": (1.0, 0.0, "m"),
    "deg/s": (0.017453292519943295, 0.0, "rad/s"),
    "rad/s": (1.0, 0.0, "rad/s"),
    "gauss": (1e-4, 0.0, "T"),
    "uT": (1e-6, 0.0, "T"),
    "T": (1.0, 0.0, "T"),
}

# Physical range limits per canonical field (SI units): (min, max).
_RANGE_LIMITS: Dict[str, tuple] = {
    "pressure": (0.0, 200_000.0),          # Pa
    "temperature": (173.15, 373.15),        # K  (-100..100 C)
    "altitude": (-500.0, 120_000.0),        # m
    "humidity": (0.0, 100.0),               # %
    "uv_index": (0.0, 20.0),
    "heading": (0.0, 360.0),                # deg
    "vertical_velocity": (-400.0, 400.0),   # m/s
    # ---- Firmware fields ----------------------------------------------
    "radiation_cpm": (0.0, 1000.0),                 # CPM
    "bme688_gas_resistance": (1000.0, 1_000_000.0), # Ohm
    "ens160_tvoc": (0.0, 60_000.0),                 # ppb
    "ens160_eco2": (400.0, 65_000.0),               # ppm
    "ens160_aqi": (1.0, 5.0),                       # AQI-UBA class
    "imu_accel_x": (-50.0, 50.0),                   # m/s^2
    "imu_accel_y": (-50.0, 50.0),
    "imu_accel_z": (-50.0, 50.0),
    # Firmware sends gyro in deg/s (+/-2000 dps full scale); UnitNormalizeStage
    # (stage 6) converts deg/s -> rad/s BEFORE this check (stage 8), so the
    # limit is expressed in SI: +/-2000 deg/s = +/-34.9 rad/s.
    "imu_gyro_x": (-math.radians(2000.0), math.radians(2000.0)),
    "imu_gyro_y": (-math.radians(2000.0), math.radians(2000.0)),
    "imu_gyro_z": (-math.radians(2000.0), math.radians(2000.0)),
    "battery_current_ma": (0.0, 2000.0),            # mA
    "mission_state_code": (0.0, 6.0),               # index into firmware STATE_NAMES
}

# Every field the firmware emits (airone_cansat.ino). Numeric fields
# become Measurements; ``mission_state`` is an enum string carried on
# ``TelemetryFrame.mission_phase`` / ``metadata["mission_state"]`` by
# ``workers.packet_processor.decode_payload_to_frame``.
FIRMWARE_FIELDS = frozenset({
    "radiation_cpm", "bme688_gas_resistance",
    "ens160_tvoc", "ens160_eco2", "ens160_aqi", "ens160_status",
    "imu_accel_x", "imu_accel_y", "imu_accel_z",
    "imu_gyro_x", "imu_gyro_y", "imu_gyro_z",
    "battery_current_ma", "mission_state", "mission_state_code",
})
STRING_ENUM_FIELDS = frozenset({"mission_state"})

# Maximum plausible rate of change per second per field (SI units).
_RATE_LIMITS: Dict[str, float] = {
    "pressure": 20_000.0,       # Pa/s
    "temperature": 20.0,        # K/s
    "altitude": 400.0,          # m/s
    "humidity": 50.0,
}


@dataclass
class PipelineContext:
    """Shared, mutable context passed to every stage."""

    mission_id: str = "default"
    mission_state: MissionState = MissionState.BOOT
    config: Dict[str, Any] = field(default_factory=dict)
    calibration_data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    filter_instances: FilterRegistry = field(default_factory=FilterRegistry)
    fusion_engine_ref: FusionEngine = field(default_factory=FusionEngine)
    hmac_key: Optional[bytes] = None
    # When True every frame that is not AUTHENTICATED (verified link tag) has
    # its measurements marked invalid — unauthenticated data is never truth.
    require_auth: bool = False
    event_bus: Optional[EventBus] = None
    # Sinks for downstream workers (queues). Set by orchestrator.
    persistence_sink: Optional[Callable[[TelemetryFrame], None]] = None
    scientific_sink: Optional[Callable[[TelemetryFrame], None]] = None
    ml_sink: Optional[Callable[[TelemetryFrame], None]] = None
    clock_drift_tolerance_ms: float = 5000.0
    # Rolling state for rate/order checks.
    _last_values: Dict[str, tuple] = field(default_factory=dict)
    _last_timestamp_us: int = 0
    _sensor_history: Dict[str, List[bool]] = field(default_factory=dict)


class PipelineStage(ABC):
    """Abstract base for a single pipeline stage."""

    stage: int = -1
    name: str = "base"

    def __init__(self) -> None:
        self.enabled = True

    @abstractmethod
    def _run(self, frame: TelemetryFrame, ctx: PipelineContext) -> None:
        """Mutate ``frame`` in place. May raise; errors are captured."""

    def process(self, frame: TelemetryFrame, ctx: PipelineContext) -> PipelineStageResult:
        start = time.perf_counter()
        m_in = len(frame.measurements)
        if not self.enabled:
            return PipelineStageResult(
                stage=self.stage, name=self.name, success=True,
                measurements_in=m_in, measurements_out=m_in,
                duration_ms=0.0, error_msg="disabled",
            )
        success = True
        error_msg = ""
        try:
            self._run(frame, ctx)
        except Exception as exc:  # noqa: BLE001 - never crash the pipeline
            success = False
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("Stage %d (%s) failed", self.stage, self.name)
        duration_ms = (time.perf_counter() - start) * 1000.0
        return PipelineStageResult(
            stage=self.stage, name=self.name, success=success,
            error_msg=error_msg, measurements_in=m_in,
            measurements_out=len(frame.measurements), duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Concrete stages
# ---------------------------------------------------------------------------
class ReceiveStage(PipelineStage):
    stage, name = 0, "RECEIVE"

    def _run(self, frame, ctx):
        frame.metadata.setdefault("received_at", utcnow().isoformat())
        frame.metadata["raw_size"] = len(frame.raw_bytes)


class AuthenticateStage(PipelineStage):
    """Frame authentication verdict.

    The truncated HMAC-SHA256 link tag is verified by the stream parser (it
    covers the on-air header+payload, which the frame object no longer has);
    the parser records the outcome in ``frame.metadata["auth_state"]``:
    ``AUTHENTICATED`` / ``UNAUTHENTICATED`` / ``UNVERIFIABLE`` /
    ``NOT_CONFIGURED`` / ``INVALID_TAG``. This stage turns that verdict into
    measurement quality:

    * ``INVALID_TAG`` or (``require_auth`` and not ``AUTHENTICATED``) →
      every measurement ``valid=False``, ``quality=INVALID``;
    * key configured but frame ``UNAUTHENTICATED`` (not required) → quality
      ``SUSPECT`` (still ``valid``), so operators see the downgrade;
    * a per-frame ``metadata["hmac"]`` hexdigest over ``raw_bytes`` is
      still honoured for in-process producers.
    """

    stage, name = 1, "AUTHENTICATE"

    def _run(self, frame, ctx):
        state = str(frame.metadata.get("auth_state", "NOT_CONFIGURED"))
        provided = frame.metadata.get("hmac")
        if provided is not None and ctx.hmac_key:
            expected = hmac.new(ctx.hmac_key, frame.raw_bytes, hashlib.sha256).hexdigest()
            state = "AUTHENTICATED" if hmac.compare_digest(expected, str(provided)) else "INVALID_TAG"
        if not ctx.hmac_key and state == "NOT_CONFIGURED":
            frame.metadata["authenticated"] = "skipped"
            frame.metadata["auth_state"] = state
            return
        frame.metadata["auth_state"] = state
        frame.metadata["authenticated"] = state == "AUTHENTICATED"
        if state == "INVALID_TAG" or (ctx.require_auth and state != "AUTHENTICATED"):
            frame.metadata["auth_rejected"] = True
            for m in frame.measurements.values():
                m.quality = QualityState.INVALID
                m.valid = False
        elif state in ("UNAUTHENTICATED", "UNVERIFIABLE"):
            for m in frame.measurements.values():
                if m.quality == QualityState.VALID:
                    m.quality = QualityState.SUSPECT


class DecodeStage(PipelineStage):
    stage, name = 2, "DECODE"

    def _run(self, frame, ctx):
        # Decoding of raw bytes into measurements is done by the receiver/parser
        # before the pipeline; here we ensure the frame carries decoded values.
        frame.metadata["decoded"] = True


class CRCValidateStage(PipelineStage):
    stage, name = 3, "CRC_VALIDATE"

    def _run(self, frame, ctx):
        if frame.crc_valid:
            return
        # CRC already failed upstream: mark everything corrupted, do not drop.
        for m in frame.measurements.values():
            m.quality = QualityState.CORRUPTED
            m.valid = False
        frame.metadata["crc_rejected"] = True


class FECRecoverStage(PipelineStage):
    stage, name = 4, "FEC_RECOVER"

    def _run(self, frame, ctx):
        if not frame.fec_applied:
            frame.metadata["fec"] = "not_present"
            return
        if not fec_mod.fec_available():
            frame.metadata["fec"] = "unavailable"
            return
        frame.metadata["fec"] = "repaired" if frame.fec_repaired else "clean"
        if frame.fec_repaired:
            for m in frame.measurements.values():
                if m.quality == QualityState.CORRUPTED:
                    m.quality = QualityState.REPAIRED


class SchemaValidateStage(PipelineStage):
    stage, name = 5, "SCHEMA_VALIDATE"

    def _run(self, frame, ctx):
        required = ctx.config.get("required_fields", [])
        present = set(frame.measurements.keys())
        missing = [f for f in required if f not in present]
        for fname in missing:
            frame.add(
                Measurement.invalid(
                    sensor_id=fname, field_name=fname,
                    quality=QualityState.MISSING, processing_stage=self.stage,
                )
            )
        frame.metadata["schema_missing"] = missing


class UnitNormalizeStage(PipelineStage):
    stage, name = 6, "UNIT_NORMALIZE"

    def _run(self, frame, ctx):
        for m in frame.measurements.values():
            conv = _UNIT_TO_SI.get(m.unit)
            if conv and m.valid:
                scale, offset, si_unit = conv
                m.value = m.value * scale + offset
                m.uncertainty = m.uncertainty * scale
                m.unit = si_unit


class TimestampValidateStage(PipelineStage):
    stage, name = 7, "TIMESTAMP_VALIDATE"

    def _run(self, frame, ctx):
        now_us = int(utcnow().timestamp() * 1e6)
        frame_us = int(frame.timestamp.timestamp() * 1e6)
        drift_us = abs(now_us - frame_us)
        tol_us = ctx.clock_drift_tolerance_ms * 1000.0
        frame.metadata["clock_drift_us"] = drift_us
        if drift_us > tol_us:
            for m in frame.measurements.values():
                if m.quality == QualityState.VALID:
                    m.quality = QualityState.STALE


class RangeCheckStage(PipelineStage):
    stage, name = 8, "RANGE_CHECK"

    def _run(self, frame, ctx):
        limits = dict(_RANGE_LIMITS)
        limits.update(ctx.config.get("range_limits", {}))
        for key, m in frame.measurements.items():
            if not m.valid:
                continue
            fld = m.field_name or key
            if fld in limits:
                lo, hi = limits[fld]
                if not (lo <= m.value <= hi):
                    m.quality = QualityState.INVALID
                    m.valid = False


class RateCheckStage(PipelineStage):
    stage, name = 9, "RATE_CHECK"

    def _run(self, frame, ctx):
        limits = dict(_RATE_LIMITS)
        limits.update(ctx.config.get("rate_limits", {}))
        now = frame.timestamp.timestamp()
        for key, m in frame.measurements.items():
            if not m.valid:
                continue
            fld = m.field_name or key
            if fld not in limits:
                continue
            prev = ctx._last_values.get(key)
            if prev is not None:
                prev_val, prev_ts = prev
                dt = now - prev_ts
                if dt > 0:
                    rate = abs(m.value - prev_val) / dt
                    if rate > limits[fld]:
                        m.quality = QualityState.SUSPECT
            ctx._last_values[key] = (m.value, now)


class DuplicateCheckStage(PipelineStage):
    stage, name = 10, "DUPLICATE_CHECK"

    def _run(self, frame, ctx):
        if frame.metadata.get("duplicate"):
            for m in frame.measurements.values():
                m.quality = QualityState.STALE
                m.valid = False
            frame.metadata["dropped_duplicate"] = True


class OrderCheckStage(PipelineStage):
    stage, name = 11, "ORDER_CHECK"

    def _run(self, frame, ctx):
        frame_us = int(frame.timestamp.timestamp() * 1e6)
        if ctx._last_timestamp_us and frame_us < ctx._last_timestamp_us:
            frame.metadata["out_of_order"] = True
        else:
            ctx._last_timestamp_us = frame_us


class SensorQualityStage(PipelineStage):
    stage, name = 12, "SENSOR_QUALITY"

    def _run(self, frame, ctx):
        for key, m in frame.measurements.items():
            hist = ctx._sensor_history.setdefault(key, [])
            hist.append(m.valid)
            if len(hist) > 50:
                del hist[0]
            trust = sum(hist) / len(hist)
            frame.metadata.setdefault("sensor_trust", {})[key] = round(trust, 4)


class CalibrationStage(PipelineStage):
    stage, name = 13, "CALIBRATION"

    def _run(self, frame, ctx):
        for key, m in frame.measurements.items():
            if not m.valid:
                continue
            cal = ctx.calibration_data.get(m.sensor_id) or ctx.calibration_data.get(key)
            if not cal:
                continue
            offset = float(cal.get("offset", 0.0))
            scale = float(cal.get("scale", 1.0))
            poly = cal.get("polynomial")
            v = m.value * scale + offset
            if poly:
                v = sum(c * (v ** i) for i, c in enumerate(poly))
            m.value = v
            m.calibration_version = str(cal.get("version", "cal-1"))
            if m.source == DataSource.RAW:
                m.source = DataSource.CALIBRATED


class FilteringStage(PipelineStage):
    stage, name = 14, "FILTERING"

    def _run(self, frame, ctx):
        ts = frame.timestamp.timestamp()
        for key, m in frame.measurements.items():
            if not m.valid:
                continue
            if ctx.filter_instances.has(key):
                flt = ctx.filter_instances.get(key)
                m.value = flt.process(m.value, ts)
                if m.source in (DataSource.RAW, DataSource.CALIBRATED):
                    m.source = DataSource.FILTERED


class SensorFusionStage(PipelineStage):
    stage, name = 15, "SENSOR_FUSION"

    def _run(self, frame, ctx):
        fe = ctx.fusion_engine_ref
        mm = frame.measurements

        def pick(*names):
            for n in names:
                if n in mm:
                    return mm[n]
            return None

        bme_p = pick("bme688_pressure", "pressure_bme688")
        bmp_p = pick("bmp581_pressure", "pressure_bmp581")
        if bme_p and bmp_p:
            frame.add(fe.fuse_pressure(bme_p, bmp_p))
        bme_t = pick("bme688_temperature", "temperature_bme688")
        bmp_t = pick("bmp581_temperature", "temperature_bmp581")
        if bme_t and bmp_t:
            frame.add(fe.fuse_temperature(bme_t, bmp_t))
        uva = pick("uva", "veml6075_uva")
        uvb = pick("uvb", "veml6075_uvb")
        if uva and uvb:
            frame.add(fe.fuse_uv_index(uva, uvb))
        pressure = frame.get("pressure") or bme_p or bmp_p
        gnss_alt = pick("gnss_altitude", "gps_altitude")
        if pressure and gnss_alt:
            frame.add(fe.fuse_altitude(pressure, gnss_alt))
        mag = pick("mag_heading", "magnetometer_heading")
        track = pick("gnss_track", "gps_track")
        if mag and track:
            frame.add(fe.fuse_heading(mag, track))


class PersistenceStage(PipelineStage):
    stage, name = 16, "PERSISTENCE"

    def _run(self, frame, ctx):
        if ctx.persistence_sink:
            ctx.persistence_sink(frame)
            frame.metadata["queued_persistence"] = True


class ScientificStage(PipelineStage):
    stage, name = 17, "SCIENTIFIC"

    def _run(self, frame, ctx):
        if ctx.scientific_sink:
            ctx.scientific_sink(frame)
            frame.metadata["queued_scientific"] = True


class MLStage(PipelineStage):
    stage, name = 18, "ML"

    def _run(self, frame, ctx):
        if ctx.ml_sink:
            ctx.ml_sink(frame)
            frame.metadata["queued_ml"] = True


class VisualizationStage(PipelineStage):
    stage, name = 19, "VISUALIZATION"

    def _run(self, frame, ctx):
        bus = ctx.event_bus or get_event_bus()
        bus.publish(TOPIC_VISUALIZATION, frame)
        frame.metadata["visualized"] = True


_STAGE_CLASSES = [
    ReceiveStage, AuthenticateStage, DecodeStage, CRCValidateStage,
    FECRecoverStage, SchemaValidateStage, UnitNormalizeStage,
    TimestampValidateStage, RangeCheckStage, RateCheckStage,
    DuplicateCheckStage, OrderCheckStage, SensorQualityStage,
    CalibrationStage, FilteringStage, SensorFusionStage,
    PersistenceStage, ScientificStage, MLStage, VisualizationStage,
]


class ProcessingPipeline:
    """Runs a telemetry frame through all 20 stages, collecting results."""

    def __init__(self, context: Optional[PipelineContext] = None) -> None:
        self.context = context or PipelineContext()
        self.stages: List[PipelineStage] = [cls() for cls in _STAGE_CLASSES]
        enabled = self.context.config.get("enabled_stages")
        if enabled is not None:
            names = set(enabled)
            for st in self.stages:
                st.enabled = st.name in names

    def get_stage(self, name: str) -> Optional[PipelineStage]:
        for st in self.stages:
            if st.name == name:
                return st
        return None

    def process(self, frame: TelemetryFrame) -> List[PipelineStageResult]:
        results: List[PipelineStageResult] = []
        for st in self.stages:
            result = st.process(frame, self.context)
            results.append(result)
            for m in frame.measurements.values():
                if m.processing_stage < st.stage:
                    m.processing_stage = st.stage
        frame.metadata["stage_results"] = [r.to_dict() for r in results]
        return results
