"""Marshmallow request schemas for API input validation.

Every schema rejects unknown fields (``unknown = RAISE``) so a client cannot
smuggle unexpected keys past validation; string fields carry explicit length
bounds and identifiers are restricted to a safe character set.
"""
from __future__ import annotations

from marshmallow import RAISE, Schema, ValidationError, fields, validate

# Identifiers that end up in filenames, log lines or SQL parameters.
SAFE_ID_REGEX = r"^[A-Za-z0-9_.\-:]{1,64}$"
_safe_id = validate.Regexp(SAFE_ID_REGEX, error="Identifier contains invalid characters")

USERNAME_REGEX = r"^[A-Za-z0-9_.\-]{1,64}$"
_username = validate.Regexp(USERNAME_REGEX, error="Invalid username")

# ISO-8601-ish timestamps only (digits, separators, timezone designators).
_iso_ts = validate.Regexp(r"^[0-9T:.\-+Z ]{4,40}$", error="Invalid timestamp")

ROLE_NAMES = ("VIEWER", "OPERATOR", "SCIENTIST", "ENGINEER", "ADMIN")


class StrictSchema(Schema):
    class Meta:
        unknown = RAISE


class LoginSchema(StrictSchema):
    username = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    password = fields.Str(required=True, validate=validate.Length(min=1, max=256))


class RefreshSchema(StrictSchema):
    refresh_token = fields.Str(required=True, validate=validate.Length(min=1, max=4096))


class ChangePasswordSchema(StrictSchema):
    """Public (throttled) endpoint: username + current password + new password."""

    username = fields.Str(required=True, validate=_username)
    current_password = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    new_password = fields.Str(required=True, validate=validate.Length(min=1, max=256))


class SetOwnPasswordSchema(StrictSchema):
    """Authenticated endpoint: current + new password for the token's subject."""

    current_password = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    new_password = fields.Str(required=True, validate=validate.Length(min=1, max=256))


class CreateUserSchema(StrictSchema):
    username = fields.Str(required=True, validate=_username)
    password = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    role = fields.Str(required=True, validate=validate.OneOf(ROLE_NAMES))
    must_change_password = fields.Bool(required=False, load_default=True)


class HistoryQuerySchema(StrictSchema):
    sensor_id = fields.Str(required=True, validate=_safe_id)
    start = fields.Str(required=False, load_default=None, validate=_iso_ts)
    end = fields.Str(required=False, load_default=None, validate=_iso_ts)
    quality = fields.Str(required=False, load_default=None, validate=validate.Length(max=32))
    limit = fields.Int(required=False, load_default=1000, validate=validate.Range(min=1, max=100000))


class ExportSchema(StrictSchema):
    sensor_id = fields.Str(required=False, load_default=None, validate=_safe_id)
    format = fields.Str(required=False, load_default="csv", validate=validate.OneOf(["csv", "json"]))
    start = fields.Str(required=False, load_default=None, validate=_iso_ts)
    end = fields.Str(required=False, load_default=None, validate=_iso_ts)


class MissionStateSchema(StrictSchema):
    new_state = fields.Str(required=True, validate=validate.Length(min=1, max=32))
    reason = fields.Str(required=False, load_default="manual override", validate=validate.Length(max=256))


class EventsQuerySchema(StrictSchema):
    limit = fields.Int(required=False, load_default=100, validate=validate.Range(min=1, max=10000))
    severity = fields.Str(required=False, load_default=None, validate=validate.Length(max=32))


class LogsQuerySchema(StrictSchema):
    n = fields.Int(required=False, load_default=100, validate=validate.Range(min=1, max=5000))


class TrainSchema(StrictSchema):
    model_type = fields.Str(required=True, validate=_safe_id)
    name = fields.Str(required=False, load_default="model", validate=_safe_id)
    parameters = fields.Dict(
        keys=fields.Str(validate=validate.Length(max=64)),
        required=False, load_default=dict,
    )


class RunAnalysisSchema(StrictSchema):
    """Body for ``POST /analysis/run/<name>``."""

    limit = fields.Int(required=False, load_default=500, validate=validate.Range(min=1, max=100000))
    mission_id = fields.Str(required=False, load_default=None, validate=_safe_id)
    params = fields.Dict(
        keys=fields.Str(validate=validate.Regexp(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")),
        required=False, load_default=dict,
    )


class ConfigUpdateSchema(StrictSchema):
    config = fields.Dict(keys=fields.Str(validate=validate.Length(max=64)), required=True)


def validate_request(schema: Schema, data: dict) -> dict:
    """Validate ``data`` against ``schema``. Raises ValidationError on failure."""

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValidationError({"_schema": ["Request body must be a JSON object"]})
    return schema.load(data)
