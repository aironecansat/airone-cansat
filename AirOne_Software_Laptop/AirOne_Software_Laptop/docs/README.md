# AirOne — Documentation

This directory is the complete operator- and engineer-facing documentation set
for the AirOne CanSat Ground Station & Scientific Analysis Platform.

Everything here reflects the **actual behaviour of the code in this
repository** — no aspirational features are documented. Where a capability is
optional or can degrade, the documentation states the exact condition and the
observable result.

## Contents

| Document | Audience | Purpose |
|----------|----------|---------|
| [operator_manual.md](operator_manual.md) | Mission operators | Day-of-mission operation, GUI, judge/presentation mode |
| [engineering_manual.md](engineering_manual.md) | Developers / integrators | Architecture, workers, capability tiers, extending the system |
| [api_reference.md](api_reference.md) | API consumers | Every REST endpoint, roles, request/response envelopes |
| [telemetry_protocol.md](telemetry_protocol.md) | Firmware / RF engineers | Wire format, CRC, FEC, sequence handling |
| [scientific_methods.md](scientific_methods.md) | Scientists / judges | The 37 analyses, their equations, assumptions, and uncertainty |
| [ml_tier.md](ml_tier.md) | Data scientists | Anomaly detectors, drift, model provenance, advisory-only guarantee |
| [failure_modes.md](failure_modes.md) | Everyone | How every subsystem degrades honestly (the quality/state vocabulary) |
| [security_model.md](security_model.md) | Security reviewers | Accounts & lockout, JWT, RBAC, telemetry HMAC link auth, audit chain, secret handling |
| [configuration.md](configuration.md) | Operators / DevOps | Every config key and its effect |
| [troubleshooting.md](troubleshooting.md) | Everyone | Symptoms → diagnosis → resolution |
| [test_plan.md](test_plan.md) | QA / judges | What the 154-test suite proves and how to run it |

## The one rule that governs everything

> **Never present fabricated, stale, or fake-complete data as real.**

Every subsystem either (a) works completely, (b) is explicitly disabled with a
visible reason, or (c) is unavailable because of a genuinely missing dependency
that is reported with a clear diagnostic. When data quality is uncertain, the
system says so using an explicit vocabulary (`VALID`, `SUSPECT`, `INVALID`,
`MISSING`, `STALE`, `CORRUPTED`, `SIMULATED`, `ESTIMATED`, `DEGRADED`,
`NOT_CONFIGURED`, `UNAVAILABLE`). See [failure_modes.md](failure_modes.md).
