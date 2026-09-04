# Configuration Reference

Configuration is a YAML file (default: `config/default_config.yaml`). Override
it with `python launcher.py --config /path/to/your.yaml`. Values not present in
your file fall back to the defaults below. Runtime edits are possible via
`PUT /api/v1/config` (ENGINEER role) after validation.

> The JWT secret is **never** in this file. It comes only from the
> `AIRONE_JWT_SECRET` environment variable.

## `api`

| Key | Default | Effect |
|-----|---------|--------|
| `host` | `127.0.0.1` | Bind address for the REST API (loopback; `AIRONE_API_HOST` overrides; `0.0.0.0` logs a warning — use a TLS proxy). |
| `port` | `5000` | API port. |
| `max_body_bytes` | `262144` | Maximum request body; larger → 413. |
| `cors_origins` | `[http://localhost:3000, http://127.0.0.1:3000]` | Exact allowed browser origins. `"*"` refused unless `AIRONE_ALLOW_CORS_WILDCARD=1`. |
| `rate_limits.auth_login` | `10 per minute` | Login rate limit. |
| `rate_limits.auth_refresh` | `30 per minute` | Token refresh limit. |
| `rate_limits.telemetry_export` | `5 per minute` | Export limit. |
| `rate_limits.ml_train` | `2 per hour` | Training limit. |
| `rate_limits.config` | `20 per minute` | Config change limit. |

## `security`

| Key | Default | Effect |
|-----|---------|--------|
| `access_token_minutes` | `15` | Access-token lifetime (hard cap 1440). Legacy key `jwt_expiry_minutes` still read. |
| `refresh_token_days` | `7` | Refresh-token lifetime (hard cap 30). Legacy key `refresh_expiry_days` still read. |
| `bcrypt_rounds` | `12` | Password hashing cost. |
| `max_login_attempts` | `5` | Per-account failures (15 min window) before lockout. |
| `max_login_attempts_per_ip` | `20` | Per-source-IP failures before lockout. |
| `lockout_seconds` | `300` | Initial lockout; doubles on repeated lockouts (cap 1 h). |

Secrets never live in this file: `AIRONE_JWT_SECRET` (required),
`AIRONE_ADMIN_PASSWORD` (bootstrap only), `AIRONE_LINK_KEY` (optional).

## `telemetry`

| Key | Default | Effect |
|-----|---------|--------|
| `serial_port` | `null` | Serial device; `null` = auto-discover. |
| `baud_rate` | `115200` | Serial baud rate. |
| `buffer_size` | `65536` | Receive buffer size (bytes). |
| `clock_drift_tolerance_ms` | `5000` | Max tolerated clock drift before a timestamp is flagged. |
| `require_authenticated_frames` | `false` | `true` requires `AIRONE_LINK_KEY` and marks every frame without a valid HMAC tag `INVALID`. |

## `pipeline`

| Key | Default | Effect |
|-----|---------|--------|
| `enabled_stages` | `null` | List of stage numbers to run; `null` = all 20. |
| `required_fields` | `[]` | Fields whose absence marks a frame incomplete. |
| `filter_config.fused_pressure` | Kalman 1-D (`process_noise` 0.001, `measurement_noise` 0.1) | Pressure filter. |
| `filter_config.temperature` | moving average (`window` 5) | Temperature filter. |
| `filter_config.altitude` | EKF (`process_noise` 0.01, `measurement_noise` 1.0) | Altitude filter. |

## `storage`

| Key | Default | Effect |
|-----|---------|--------|
| `db_path` | `data/airone.db` | SQLite database path. |
| `data_dir` | `data` | Data directory (exports, snapshots, models). |
| `backup_interval_hours` | `6` | DB backup cadence. |
| `max_db_size_mb` | `2048` | Soft size ceiling. |

## `mission`

| Key | Default | Effect |
|-----|---------|--------|
| `default_primary` | `atmospheric_profiling` | Primary objective. |
| `default_secondary` | `uv_radiation_mapping` | Secondary objective. |

## `logging`

| Key | Default | Effect |
|-----|---------|--------|
| `level` | `INFO` | Log level. |
| `log_dir` | `logs` | Log directory. |
| `json_output` | `true` | Structured JSON logs. |

## `ml` (optional block)

Not present in the default file; add it to tune the ML worker.

| Key | Default | Effect |
|-----|---------|--------|
| `window_size` | `240` | Rolling window (frames) for live anomaly scoring. |
| `run_interval_s` | `5.0` | Seconds between scoring passes. |
| `min_samples` | `20` | Minimum valid samples before scoring/training. |
| `auto_fit` | `true` | Auto-fit the ensemble on the current window. |
| `model_dir` | `data/models` | Where checksummed model artefacts are stored. |
| `features` | default 6-feature set | Feature columns to extract. |

## Other top-level keys

| Key | Default | Effect |
|-----|---------|--------|
| `mission_id` | `default` | Mission id used for storage/queries. |

`admin_password` / `viewer_password` are **no longer read**: accounts are
created with `launcher.py --create-admin` / `--create-user` (see
[security_model.md](security_model.md)).

## Runtime-mutable keys

`PUT /api/v1/config` accepts only `mission_id`, `objectives`,
`default_primary`, `default_secondary`, `pipeline`, `scientific`, `ml`, `gui`,
`simulation`, `logging`. `api`, `security`, `storage` and `telemetry` are
refused with 400 — change them in the file and restart.
