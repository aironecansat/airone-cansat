"""Canonical exception hierarchy for AirOne V7.1.

Every recoverable failure raises a specific, descriptive exception. No
silent failures are permitted anywhere in the critical path.
"""
from __future__ import annotations


class AirOneError(Exception):
    """Base class for all AirOne errors."""


# --- Telemetry / protocol ---------------------------------------------------
class FrameError(AirOneError):
    """Raised when a binary telemetry frame is malformed or fails validation."""


class CRCError(FrameError):
    """Raised when a frame's CRC32 does not match the computed value."""


class MagicNotFoundError(FrameError):
    """Raised when no MAGIC preamble is found in a buffer."""


class PayloadTooLargeError(FrameError):
    """Raised when the declared payload length exceeds the allowed maximum."""


# --- Security ---------------------------------------------------------------
class SecurityError(AirOneError):
    """Base class for security-related errors."""


class InsecureConfigError(SecurityError):
    """Raised at startup when security configuration is unsafe for production."""


class TokenExpiredError(SecurityError):
    """Raised when a JWT has expired."""


class InvalidTokenError(SecurityError):
    """Raised when a JWT is malformed, revoked, or otherwise invalid."""


class PermissionDeniedError(SecurityError):
    """Raised when an authenticated principal lacks a required permission."""


# --- Mission state ----------------------------------------------------------
class MissionError(AirOneError):
    """Base class for mission-state errors."""


class InvalidTransitionError(MissionError):
    """Raised when a mission state transition is not permitted."""


# --- Storage ----------------------------------------------------------------
class StorageError(AirOneError):
    """Base class for storage/database errors."""


class MigrationError(StorageError):
    """Raised when a database migration fails."""


# --- Pipeline ---------------------------------------------------------------
class PipelineError(AirOneError):
    """Base class for data-processing pipeline errors."""


class ConfigError(AirOneError):
    """Raised when configuration is invalid or missing required keys."""
