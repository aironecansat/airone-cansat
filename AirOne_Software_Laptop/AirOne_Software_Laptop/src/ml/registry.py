"""On-disk model store with checksum-validated safe loading.

Models are serialized with joblib. On save we record the SHA-256 of the
written file. On load we recompute the hash and refuse to unpickle if it does
not match the expected hash recorded in the model registry — a tampered,
truncated, or swapped artefact raises :class:`SafeLoadError` instead of
executing untrusted pickle bytes.

This does not make pickle universally safe, but it guarantees that only the
exact artefact this system produced (and recorded) is ever loaded.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .provenance import sha256_of_file

logger = logging.getLogger(__name__)

try:
    import joblib
    _JOBLIB_OK, _JOBLIB_ERR = True, ""
except Exception as exc:  # pragma: no cover
    joblib = None  # type: ignore
    _JOBLIB_OK, _JOBLIB_ERR = False, str(exc)


class SafeLoadError(RuntimeError):
    """Raised when a model artefact fails integrity verification."""


class ModelStore:
    def __init__(self, directory: str = "data/models") -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    @property
    def available(self) -> bool:
        return _JOBLIB_OK

    def save(self, obj: Any, name: str, version: str) -> tuple[str, str]:
        """Serialize ``obj`` and return (path, sha256). Raises if joblib absent."""
        if not _JOBLIB_OK:
            raise SafeLoadError(f"joblib unavailable: {_JOBLIB_ERR}")
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        path = os.path.join(self.directory, f"{safe_name}__{version}.joblib")
        joblib.dump(obj, path)
        digest = sha256_of_file(path)
        logger.info("Saved model %s v%s (%s) sha256=%s", name, version, path, digest[:12])
        return path, digest

    def load(self, path: str, expected_sha256: Optional[str]) -> Any:
        """Load a model only if its on-disk hash matches ``expected_sha256``."""
        if not _JOBLIB_OK:
            raise SafeLoadError(f"joblib unavailable: {_JOBLIB_ERR}")
        if not os.path.exists(path):
            raise SafeLoadError(f"model file missing: {path}")
        if not expected_sha256:
            raise SafeLoadError(
                "no expected checksum recorded; refusing to load unverifiable artefact"
            )
        actual = sha256_of_file(path)
        if actual != expected_sha256:
            raise SafeLoadError(
                f"checksum mismatch for {path}: expected {expected_sha256[:12]}…, "
                f"got {actual[:12]}… — refusing to load (possible tampering)"
            )
        return joblib.load(path)
