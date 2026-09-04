"""Tamper-evident, structured JSON audit log.

Each entry stores the SHA256 of the previous entry plus its own checksum,
forming a hash chain: altering any past entry invalidates every subsequent
checksum. Entries are written as JSON lines to a rotating file handler.

The chain is **continuous across process restarts**: on start-up the logger
reads the last entry of the existing file and continues from its checksum, so
a gap or edit between sessions is detectable. :func:`verify_audit_file` (also
exposed as ``python3 -m src.security.audit <file>`` and
``launcher.py --verify-audit``) re-walks a file and reports the first broken
link. Entries deliberately never contain passwords or tokens.

Limitation (documented, not hidden): a hash chain proves *integrity* of the
sequence, not *authenticity* — an attacker with write access to the file and
knowledge of the format can rewrite the whole chain from a chosen point. Ship
the file to a remote collector (or sign it) for a stronger guarantee.
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class AuditEvent(str, Enum):
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    LOGOUT = "LOGOUT"
    TOKEN_REFRESH = "TOKEN_REFRESH"
    TOKEN_REVOKE = "TOKEN_REVOKE"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    MISSION_STATE_CHANGE = "MISSION_STATE_CHANGE"
    MODEL_LOAD = "MODEL_LOAD"
    MODEL_TRAIN = "MODEL_TRAIN"
    DATA_DELETE = "DATA_DELETE"
    DATA_EXPORT = "DATA_EXPORT"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    RATE_LIMIT_HIT = "RATE_LIMIT_HIT"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PASSWORD_CHANGE = "PASSWORD_CHANGE"
    USER_CREATE = "USER_CREATE"
    USER_DELETE = "USER_DELETE"
    USER_DISABLE = "USER_DISABLE"
    USER_ENABLE = "USER_ENABLE"
    TELEMETRY_AUTH_FAILURE = "TELEMETRY_AUTH_FAILURE"


_GENESIS = "0" * 64


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditLogger:
    """Thread-safe append-only audit logger with a hash chain."""

    def __init__(
        self,
        log_dir: str = "logs",
        filename: str = "audit.jsonl",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 10,
    ) -> None:
        os.makedirs(log_dir, exist_ok=True)
        self._path = os.path.join(log_dir, filename)
        self._lock = threading.Lock()
        self._previous_checksum = _load_last_checksum(self._path)
        self._start_checksum = self._previous_checksum
        self._entries_in_memory: List[Dict[str, Any]] = []

        self._logger = logging.getLogger("airone.audit")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # Avoid duplicate handlers if constructed twice.
        if not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(h, "baseFilename", "") == os.path.abspath(self._path)
            for h in self._logger.handlers
        ):
            handler = logging.handlers.RotatingFileHandler(
                self._path, maxBytes=max_bytes, backupCount=backup_count
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def log(
        self,
        event_type: AuditEvent,
        user_id: Optional[str] = None,
        role: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        result: str = "success",
        request_ip: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append an audit entry and return it (including its checksum)."""

        with self._lock:
            entry: Dict[str, Any] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type.value if isinstance(event_type, AuditEvent) else str(event_type),
                "user_id": user_id,
                "role": role,
                "ip": request_ip,
                "endpoint": endpoint,
                "result": result,
                "details": details or {},
                "previous_checksum": self._previous_checksum,
            }
            # Checksum covers the canonical JSON of the entry (excluding its own checksum).
            serialized = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            entry_checksum = _sha256(serialized)
            entry["entry_checksum"] = entry_checksum
            self._previous_checksum = entry_checksum

            self._logger.info(json.dumps(entry, separators=(",", ":")))
            self._entries_in_memory.append(entry)
            return entry

    @property
    def last_checksum(self) -> str:
        with self._lock:
            return self._previous_checksum

    @property
    def path(self) -> str:
        return self._path

    def verify_file(self) -> "AuditVerification":
        """Verify the whole on-disk file, including previous sessions."""

        return verify_audit_file(self._path)

    def entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._entries_in_memory)

    def verify_chain(self) -> bool:
        """Recompute the in-memory chain (this session's entries)."""

        with self._lock:
            prev = self._start_checksum
            for entry in self._entries_in_memory:
                if entry["previous_checksum"] != prev:
                    return False
                copy = {k: v for k, v in entry.items() if k != "entry_checksum"}
                serialized = json.dumps(copy, sort_keys=True, separators=(",", ":"))
                if _sha256(serialized) != entry["entry_checksum"]:
                    return False
                prev = entry["entry_checksum"]
            return True


def _load_last_checksum(path: str) -> str:
    """Return the ``entry_checksum`` of the last well-formed line, or GENESIS."""

    if not os.path.exists(path):
        return _GENESIS
    last = _GENESIS
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("entry_checksum"), str):
                    last = entry["entry_checksum"]
    except OSError:
        return _GENESIS
    return last


@dataclass
class AuditVerification:
    ok: bool
    entries: int
    first_bad_line: Optional[int] = None  # 1-based line number
    reason: Optional[str] = None
    last_checksum: str = _GENESIS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "entries": self.entries,
            "first_bad_line": self.first_bad_line,
            "reason": self.reason,
            "last_checksum": self.last_checksum,
        }


def verify_audit_file(path: str) -> AuditVerification:
    """Walk an audit JSONL file and verify the hash chain end-to-end.

    Detects: edited entries (checksum mismatch), deleted/inserted entries
    (previous_checksum mismatch), truncated/garbled lines.
    """

    if not os.path.exists(path):
        return AuditVerification(False, 0, None, "file not found")
    prev = _GENESIS
    count = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return AuditVerification(False, count, lineno, "malformed JSON", prev)
            if not isinstance(entry, dict) or "entry_checksum" not in entry:
                return AuditVerification(False, count, lineno, "missing entry_checksum", prev)
            if entry.get("previous_checksum") != prev:
                return AuditVerification(False, count, lineno, "chain break (previous_checksum mismatch)", prev)
            copy = {k: v for k, v in entry.items() if k != "entry_checksum"}
            serialized = json.dumps(copy, sort_keys=True, separators=(",", ":"))
            if _sha256(serialized) != entry["entry_checksum"]:
                return AuditVerification(False, count, lineno, "entry checksum mismatch (tampered)", prev)
            prev = entry["entry_checksum"]
            count += 1
    return AuditVerification(True, count, None, None, prev)


_DEFAULT_AUDIT: Optional[AuditLogger] = None
_AUDIT_LOCK = threading.Lock()


def get_audit_logger(log_dir: str = "logs") -> AuditLogger:
    global _DEFAULT_AUDIT
    with _AUDIT_LOCK:
        if _DEFAULT_AUDIT is None:
            _DEFAULT_AUDIT = AuditLogger(log_dir=log_dir)
        return _DEFAULT_AUDIT


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - thin CLI
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Verify an AirOne audit log hash chain")
    parser.add_argument("path", nargs="?", default="logs/audit.jsonl")
    args = parser.parse_args(argv)
    result = verify_audit_file(args.path)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
