"""Provenance and integrity hashing for ML artefacts.

Every persisted model records the SHA-256 of its serialized bytes and a hash
of the exact training-data matrix. Loading verifies the file hash before
unpickling so a tampered or truncated artefact is refused rather than
silently executed.
"""
from __future__ import annotations

import hashlib
from typing import Sequence


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_array(matrix: Sequence[Sequence[float]], feature_names: Sequence[str]) -> str:
    """Deterministic hash of a 2-D training matrix + its feature order.

    Uses a stable textual encoding rounded to 6 significant figures so tiny
    float representation differences do not change the hash while genuine data
    changes do. Works with or without NumPy.
    """
    h = hashlib.sha256()
    h.update(("|".join(str(f) for f in feature_names)).encode("utf-8"))
    h.update(b"\n")
    try:
        import numpy as np

        arr = np.asarray(matrix, dtype=float)
        h.update(str(arr.shape).encode("utf-8"))
        # Round to stabilise, then hash the raw bytes.
        rounded = np.round(arr, 6)
        h.update(np.ascontiguousarray(rounded).tobytes())
    except Exception:  # noqa: BLE001 - pure-python fallback
        for row in matrix:
            h.update(("," .join(f"{float(v):.6g}" for v in row)).encode("utf-8"))
            h.update(b";")
    return h.hexdigest()
