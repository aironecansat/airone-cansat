"""Scientific analysis endpoints.

Exposes the analysis catalogue, live results from the running scientific
worker, on-demand execution over the stored measurement window, the mission
narrative (judge mode), and the fused-measurement view.

Honesty is preserved end-to-end: when the analysis tier is not running the API
returns an explicit ``NOT_CONFIGURED`` state instead of empty-but-successful
data, and invalid results are surfaced with their reason rather than hidden.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, request
from marshmallow import ValidationError

from ...core.models import TelemetryFrame
from ...scientific import build_default_registry
from ...scientific.narrative import build_narrative
from ...scientific.series import SeriesBundle
from ...security.rbac import Role, require_role
from ..middleware import envelope, jwt_required
from ..schemas import RunAnalysisSchema, validate_request

logger = logging.getLogger(__name__)
bp = Blueprint("analysis", __name__, url_prefix="/api/v1")

# A registry for describing the catalogue and for on-demand runs when no worker
# is attached (e.g. running the API standalone against stored data).
_REGISTRY = build_default_registry()


def _services():
    return current_app.config["SERVICES"]


@bp.get("/analysis")
@jwt_required
@require_role(Role.VIEWER)
def list_analyses():
    """List the analysis catalogue grouped by category."""

    return envelope(True, data={
        "categories": _REGISTRY.categories(),
        "analyses": _REGISTRY.describe(),
        "count": len(_REGISTRY.names()),
    })


@bp.get("/analysis/results")
@jwt_required
@require_role(Role.VIEWER)
def latest_results():
    """Return the latest computed results from the live scientific worker."""

    svc = _services()
    worker = getattr(svc, "scientific_worker", None)
    if worker is None:
        return envelope(
            False,
            error="Scientific analysis tier is not running",
            data={"state": "NOT_CONFIGURED"},
            status=503,
        )
    return envelope(True, data={"state": "AVAILABLE", "results": worker.latest_results()})


@bp.get("/analysis/<name>")
@jwt_required
@require_role(Role.VIEWER)
def get_analysis(name: str):
    """Return the latest live result for one analysis, or its spec."""

    spec = _REGISTRY.get(name)
    if spec is None:
        return envelope(False, error=f"Unknown analysis '{name}'", status=404)
    svc = _services()
    worker = getattr(svc, "scientific_worker", None)
    live = None
    if worker is not None:
        live = worker.latest_results().get(name)
    return envelope(True, data={
        "spec": {
            "name": spec.name,
            "category": spec.category,
            "description": spec.description,
            "required_fields": spec.required_fields,
            "min_samples": spec.min_samples,
        },
        "latest_result": live,
    })


@bp.post("/analysis/<name>/run")
@jwt_required
@require_role(Role.SCIENTIST)
def run_analysis(name: str):
    """Run one analysis on demand over the stored measurement window.

    Requires SCIENTIST or higher. Builds a SeriesBundle from persisted
    measurements for the requested sensors/window.
    """

    spec = _REGISTRY.get(name)
    if spec is None:
        return envelope(False, error=f"Unknown analysis '{name}'", status=404)
    svc = _services()
    try:
        body = validate_request(RunAnalysisSchema(), request.get_json(silent=True) or {})
    except ValidationError as exc:
        return envelope(False, error=f"Validation error: {exc.messages}", status=400)
    mission_id = body["mission_id"] or str(svc.config.get("mission_id", "default"))
    limit = body["limit"]

    frames = _frames_from_storage(svc, mission_id, limit)
    if not frames:
        return envelope(False, error="No stored measurements to analyse",
                        data={"state": "UNAVAILABLE"}, status=422)
    series = SeriesBundle(frames)
    params = body["params"]
    try:
        result = _REGISTRY.run(name, series, **params)
    except TypeError:
        # Unknown keyword parameters for this analysis: a client error, not a crash.
        return envelope(False, error="Unsupported analysis parameters", status=400)
    # Persist the on-demand result too.
    try:
        if result.valid and result.value is not None:
            svc.analysis_repo.store(result, mission_id=mission_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist on-demand analysis result")
    return envelope(True, data={"result": result.to_dict()})


@bp.get("/narrative")
@jwt_required
@require_role(Role.VIEWER)
def narrative():
    """Return the mission narrative (judge / presentation mode)."""

    svc = _services()
    worker = getattr(svc, "scientific_worker", None)
    if worker is not None and worker.latest_narrative():
        return envelope(True, data=worker.latest_narrative())
    # Fall back to computing from stored data on demand.
    mission_id = str(request.args.get("mission_id", svc.config.get("mission_id", "default")))
    frames = _frames_from_storage(svc, mission_id, 500)
    if not frames:
        return envelope(False, error="No data for narrative",
                        data={"state": "UNAVAILABLE"}, status=422)
    series = SeriesBundle(frames)
    results = {n: _REGISTRY.run(n, series) for n in _REGISTRY.names()}
    nar = build_narrative(results, mission_state=svc.mission_machine.state.value, mission_id=mission_id)
    return envelope(True, data=nar)


@bp.get("/fusion")
@jwt_required
@require_role(Role.VIEWER)
def fusion():
    """Return the latest fused measurements (pressure, temperature, etc.)."""

    svc = _services()
    latest = svc.telemetry_repo.latest_per_sensor(
        str(request.args.get("mission_id", svc.config.get("mission_id", "default")))
    )
    fused = [m.to_dict() for m in latest if m.source.value == "FUSED"]
    return envelope(True, data={
        "fused_measurements": fused,
        "count": len(fused),
        "state": "AVAILABLE" if fused else "UNAVAILABLE",
    })


def _frames_from_storage(svc, mission_id: str, limit: int):
    """Reconstruct TelemetryFrames from persisted measurements.

    Measurements are grouped by their (isoformat) timestamp into frames so the
    analyses receive a time-ordered window. Only valid rows are used for the
    series; invalid ones are represented so quality accounting stays honest.
    """

    rows = svc.db.query(
        """SELECT * FROM measurements WHERE mission_id = ?
           ORDER BY timestamp ASC LIMIT ?""",
        (mission_id, limit),
    )
    if not rows:
        return []
    from ...storage.repositories.telemetry_repo import TelemetryRepository

    by_ts = {}
    for r in rows:
        m = TelemetryRepository._row_to_measurement(r)
        frame = by_ts.get(r["timestamp"])
        if frame is None:
            frame = TelemetryFrame(timestamp=m.timestamp)
            by_ts[r["timestamp"]] = frame
        frame.add(m)
    return [by_ts[k] for k in sorted(by_ts)]
