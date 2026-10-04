"""Canonical data models for AirOne.

The :class:`Measurement` object is the single source of truth for any value
that flows through the system. It always carries quality, provenance, and
uncertainty. A sensor failure NEVER produces a silent ``0.0`` — it produces a
``Measurement`` with ``valid=False`` and ``quality=QualityState.INVALID``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class QualityState(str, Enum):
    """Quality classification for a single measurement."""

    VALID = "VALID"
    SUSPECT = "SUSPECT"
    INVALID = "INVALID"
    MISSING = "MISSING"
    STALE = "STALE"
    CORRUPTED = "CORRUPTED"
    REPAIRED = "REPAIRED"
    SIMULATED = "SIMULATED"
    ESTIMATED = "ESTIMATED"


class DataSource(str, Enum):
    """Provenance of a measurement value."""

    RAW = "RAW"
    CALIBRATED = "CALIBRATED"
    FILTERED = "FILTERED"
    FUSED = "FUSED"
    ESTIMATED = "ESTIMATED"
    SIMULATED = "SIMULATED"


class MissionState(str, Enum):
    """Deterministic mission phases."""

    BOOT = "BOOT"
    SELF_TEST = "SELF_TEST"
    PRELAUNCH = "PRELAUNCH"
    ASCENT = "ASCENT"
    APOGEE = "APOGEE"
    DESCENT = "DESCENT"
    LANDED = "LANDED"
    SAFE = "SAFE"
    FAULT = "FAULT"


class EventSeverity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime``."""

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Core measurement model
# ---------------------------------------------------------------------------
@dataclass
class Measurement:
    """A single, fully-provenanced measurement.

    Attributes are never left implicit. ``valid`` and ``quality`` must always
    agree with the numeric ``value``: an INVALID measurement carries a float
    value (often ``nan``) but ``valid`` is ``False`` and consumers must not
    treat the number as real.
    """

    value: float
    unit: str
    timestamp: datetime
    sensor_id: str
    quality: QualityState = QualityState.VALID
    valid: bool = True
    uncertainty: float = 0.0
    calibration_version: str = "none"
    source: DataSource = DataSource.RAW
    processing_stage: int = 0
    field_name: str = ""

    def __post_init__(self) -> None:
        # Enforce coherence between valid flag and quality state.
        if self.quality in (
            QualityState.INVALID,
            QualityState.MISSING,
            QualityState.CORRUPTED,
        ):
            self.valid = False
        # Ensure timestamp is timezone-aware UTC.
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)

    @classmethod
    def invalid(
        cls,
        sensor_id: str,
        unit: str = "",
        field_name: str = "",
        quality: QualityState = QualityState.INVALID,
        timestamp: Optional[datetime] = None,
        processing_stage: int = 0,
    ) -> "Measurement":
        """Construct an explicitly invalid measurement.

        Used wherever a sensor fails — never return a bare ``0.0``.
        """

        return cls(
            value=float("nan"),
            unit=unit,
            timestamp=timestamp or utcnow(),
            sensor_id=sensor_id,
            quality=quality,
            valid=False,
            uncertainty=float("inf"),
            source=DataSource.RAW,
            processing_stage=processing_stage,
            field_name=field_name,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["quality"] = self.quality.value
        d["source"] = self.source.value
        return d


@dataclass
class TelemetryFrame:
    """A collection of measurements decoded from one telemetry packet."""

    sequence: int = 0
    timestamp: datetime = field(default_factory=utcnow)
    measurements: Dict[str, Measurement] = field(default_factory=dict)
    packet_type: int = 0
    raw_bytes: bytes = b""
    crc_valid: bool = True
    fec_applied: bool = False
    fec_repaired: bool = False
    mission_phase: str = MissionState.BOOT.value
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add(self, measurement: Measurement) -> None:
        key = measurement.field_name or measurement.sensor_id
        self.measurements[key] = measurement

    def get(self, key: str) -> Optional[Measurement]:
        return self.measurements.get(key)

    def valid_measurements(self) -> Dict[str, Measurement]:
        return {k: m for k, m in self.measurements.items() if m.valid}


# ---------------------------------------------------------------------------
# Mission transitions
# ---------------------------------------------------------------------------
@dataclass
class MissionTransition:
    timestamp: datetime
    prev_state: MissionState
    new_state: MissionState
    reason: str
    supporting_measurements: List[Measurement] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "prev_state": self.prev_state.value,
            "new_state": self.new_state.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "supporting_measurements": [
                m.to_dict() for m in self.supporting_measurements
            ],
        }


# ---------------------------------------------------------------------------
# Data quality summary
# ---------------------------------------------------------------------------
@dataclass
class DataQuality:
    overall_score: float = 1.0
    per_sensor_scores: Dict[str, float] = field(default_factory=dict)
    sample_count: int = 0
    invalid_count: int = 0
    missing_count: int = 0
    imputed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pipeline stage result
# ---------------------------------------------------------------------------
@dataclass
class PipelineStageResult:
    stage: int
    name: str
    success: bool
    error_msg: str = ""
    measurements_in: int = 0
    measurements_out: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------
@dataclass
class Event:
    timestamp: datetime
    event_type: str
    severity: EventSeverity
    source: str
    message: str
    mission_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    acknowledged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["severity"] = self.severity.value
        return d
