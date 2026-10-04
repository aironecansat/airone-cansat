"""Reed-Solomon forward error correction wrapper.

FEC is optional. If the ``reedsolo`` package is not installed, FEC is disabled
with a clear log warning and payloads pass through unchanged. When available,
RS(255,223) is used: 32 parity bytes per 223-byte block, able to correct up to
16 byte errors per block.
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

# RS(255, 223): 32 parity symbols.
RS_N = 255
RS_K = 223
RS_NSYM = RS_N - RS_K  # 32

try:  # pragma: no cover - availability depends on environment
    import reedsolo  # type: ignore

    _RS_AVAILABLE = True
except Exception:  # noqa: BLE001
    reedsolo = None  # type: ignore
    _RS_AVAILABLE = False
    logger.warning(
        "reedsolo not installed: Reed-Solomon FEC is DISABLED. "
        "Install 'reedsolo' to enable error correction."
    )


def fec_available() -> bool:
    """Return True if Reed-Solomon FEC can be used."""

    return _RS_AVAILABLE


def _codec() -> "reedsolo.RSCodec":
    return reedsolo.RSCodec(RS_NSYM)


def encode_payload(data: bytes) -> bytes:
    """RS(255,223)-encode ``data``.

    If FEC is unavailable the input is returned unchanged (so the caller must
    not set the FEC flag). reedsolo handles chunking into 255-byte blocks
    internally.
    """

    if not _RS_AVAILABLE:
        return data
    return bytes(_codec().encode(bytearray(data)))


def decode_payload(data: bytes) -> Tuple[bytes, bool]:
    """Attempt to RS-decode ``data``.

    Returns ``(decoded_bytes, was_repaired)``. On unrecoverable corruption the
    original bytes are returned with ``was_repaired=False`` so the caller can
    mark the measurement ``CORRUPTED`` rather than fabricate data.
    """

    if not _RS_AVAILABLE:
        return data, False
    try:
        decoded, _, errata = _codec().decode(bytearray(data))
        was_repaired = bool(errata)
        return bytes(decoded), was_repaired
    except reedsolo.ReedSolomonError as exc:  # type: ignore[attr-defined]
        logger.warning("Reed-Solomon decode failed, preserving raw payload: %s", exc)
        return data, False
