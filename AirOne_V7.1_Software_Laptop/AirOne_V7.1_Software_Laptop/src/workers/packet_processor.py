"""Packet processor worker: decodes payloads and runs the 20-stage pipeline.

The Phase 1 payload convention is a JSON object mapping ``field_name`` to
``{"value": float, "unit": str, "sensor_id": str}``. Binary sensor structs can
be added in later phases without changing the pipeline.

Payload hardening (Team AirOne security audit) — the payload arrives over an
unauthenticated radio link and is treated as hostile:

* size cap (``MAX_JSON_BYTES``) checked *before* ``json.loads``;
* nesting depth cap (``MAX_JSON_DEPTH``) and field-count cap (``MAX_FIELDS``);
* top-level must be a JSON object; anything else → frame decoded empty and
  ``metadata["payload_error"]`` set (explicit, never a crash);
* ``field``/``unit``/``sensor_id`` must be strings within ``MAX_STR_LEN`` and
  made of printable characters; ``value`` must be a finite number or ``null``
  (→ INVALID measurement) — booleans, strings, nested objects are **INVALID**;
* unknown per-field keys are ignored but counted in ``metadata["unknown_keys"]``.

A malformed payload never raises out of :func:`decode_payload_to_frame`; the
outcome is always an explicit state on the frame.
"""
from __future__ import annotations

import json
import logging
import math
import queue
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from ..core.models import DataSource, Measurement, QualityState, TelemetryFrame
from ..data_processing.pipeline import PipelineContext, ProcessingPipeline
from ..telemetry.parser import ParsedPacket
from .base_worker import BaseWorker

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 16 * 1024
MAX_JSON_DEPTH = 3
MAX_FIELDS = 64
MAX_STR_LEN = 64
KNOWN_SPEC_KEYS = frozenset({"value", "unit", "sensor_id", "quality", "source"})
# Timestamps outside this window are implausible for a mission and replaced by
# the receive time (recorded in metadata) so a hostile TS cannot poison
# time-ordered analyses.
_TS_MIN_US = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1e6)
_TS_MAX_US = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1e6)


def _json_depth(obj: Any, depth: int = 1) -> int:
    if isinstance(obj, dict):
        return max([depth] + [_json_depth(v, depth + 1) for v in obj.values()])
    if isinstance(obj, list):
        return max([depth] + [_json_depth(v, depth + 1) for v in obj])
    return depth


def _safe_str(value: Any, fallback: str) -> Tuple[str, bool]:
    """Return ``(string, ok)``; ``ok`` False if the value was not an acceptable string."""

    if not isinstance(value, str):
        return fallback, False
    if not (0 < len(value) <= MAX_STR_LEN) or not value.isprintable():
        return fallback, False
    return value, True


def _coerce_value(raw: Any) -> Tuple[float, bool]:
    """Return ``(value, valid)``. Only finite int/float are valid; ``None`` is an
    explicit missing value; everything else is INVALID."""

    if raw is None:
        return float("nan"), False
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        return float("nan"), False
    if isinstance(raw, (int, float)):
        try:
            v = float(raw)
        except (OverflowError, ValueError):
            return float("nan"), False
        if not math.isfinite(v):
            return float("nan"), False
        return v, True
    return float("nan"), False


def _parse_payload(payload: bytes) -> Tuple[Dict[str, Any], str]:
    """Parse and structurally validate a payload. Returns ``(obj, error)``."""

    if not payload:
        return {}, ""
    if len(payload) > MAX_JSON_BYTES:
        return {}, f"payload {len(payload)} bytes exceeds {MAX_JSON_BYTES}"
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {}, "payload is not valid UTF-8"
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return {}, "payload is not valid JSON"
    if not isinstance(obj, dict):
        return {}, "payload top-level must be a JSON object"
    if len(obj) > MAX_FIELDS:
        return {}, f"payload has {len(obj)} fields, limit {MAX_FIELDS}"
    if _json_depth(obj) > MAX_JSON_DEPTH:
        return {}, f"payload nesting exceeds depth {MAX_JSON_DEPTH}"
    return obj, ""


def decode_payload_to_frame(pkt: ParsedPacket) -> TelemetryFrame:
    """Decode a parsed packet's payload into a :class:`TelemetryFrame`.

    Never raises on hostile payloads; problems are recorded on the frame.
    """

    now = datetime.now(timezone.utc)
    ts_us = int(pkt.header.get("timestamp_us", 0) or 0)
    ts_plausible = _TS_MIN_US <= ts_us <= _TS_MAX_US
    ts = datetime.fromtimestamp(ts_us / 1e6, tz=timezone.utc) if ts_plausible else now
    frame = TelemetryFrame(
        sequence=pkt.sequence,
        timestamp=ts,
        packet_type=int(pkt.header.get("packet_type", 0)),
        raw_bytes=pkt.payload,
        crc_valid=pkt.crc_valid,
        fec_applied=bool(pkt.header.get("fec_present", False)),
    )
    frame.metadata["duplicate"] = pkt.duplicate
    frame.metadata["out_of_order"] = pkt.out_of_order
    frame.metadata["replay"] = pkt.replay
    frame.metadata["auth_state"] = pkt.auth_state
    frame.metadata["auth_rejected"] = pkt.rejected
    if not ts_plausible:
        frame.metadata["timestamp_replaced"] = True
        frame.metadata["timestamp_us_declared"] = ts_us

    if not pkt.crc_valid:
        # Corrupted packet (bad CRC or bad auth tag): record it but attach no
        # fabricated measurements.
        return frame

    obj, error = _parse_payload(pkt.payload)
    if error:
        frame.metadata["payload_error"] = error
        logger.warning("Payload rejected seq=%d: %s", pkt.sequence, error)
        return frame

    unknown_keys = 0
    invalid_fields = 0
    for raw_name, spec in obj.items():
        field_name, ok_name = _safe_str(raw_name, "")
        if not ok_name:
            invalid_fields += 1
            continue
        source = DataSource.RAW
        quality = QualityState.VALID
        if isinstance(spec, dict):
            unknown_keys += sum(1 for k in spec if k not in KNOWN_SPEC_KEYS)
            value, valid = _coerce_value(spec.get("value"))
            unit, ok_unit = _safe_str(spec.get("unit", ""), "") if spec.get("unit", "") != "" else ("", True)
            sensor_id, ok_sid = _safe_str(spec.get("sensor_id", field_name), field_name)
            if not (ok_unit and ok_sid):
                valid = False
            # Provenance downgrade only: a payload may declare itself SIMULATED
            # (the simulator does), it can never upgrade its own trust level.
            if spec.get("source") == DataSource.SIMULATED.value:
                source = DataSource.SIMULATED
                quality = QualityState.SIMULATED
        else:
            value, valid = _coerce_value(spec)
            unit = ""
            sensor_id = field_name
        if not valid:
            invalid_fields += 1
            quality = QualityState.INVALID
        frame.add(
            Measurement(
                value=value, unit=unit, timestamp=ts, sensor_id=sensor_id,
                quality=quality, valid=valid, source=source,
                field_name=field_name,
            )
        )
    if unknown_keys:
        frame.metadata["unknown_keys"] = unknown_keys
    if invalid_fields:
        frame.metadata["invalid_fields"] = invalid_fields
    return frame


class PacketProcessorWorker(BaseWorker):
    def __init__(
        self,
        packet_queue: "queue.Queue",
        context: PipelineContext,
    ) -> None:
        super().__init__("PacketProcessor")
        self.packet_queue = packet_queue
        self.pipeline = ProcessingPipeline(context)
        self.health_status["payload_rejected"] = 0
        self.health_status["auth_rejected"] = 0

    def run(self) -> None:
        while not self.should_stop():
            try:
                pkt = self.packet_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.health_status["iterations"] += 1
            try:
                frame = decode_payload_to_frame(pkt)
                if frame.metadata.get("payload_error"):
                    self.health_status["payload_rejected"] += 1
                if frame.metadata.get("auth_rejected"):
                    self.health_status["auth_rejected"] += 1
                self.pipeline.process(frame)
            except Exception:  # noqa: BLE001
                self.health_status["errors"] += 1
                logger.exception("Packet processing failed for seq=%s", pkt.sequence)
