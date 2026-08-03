"""In-memory registry of validation runs + background execution.

Same pattern as backend/migration/runs.py: a daemon thread does the work and
updates a shared state the API polls. Process-memory state is fine for a
single-user accelerator App.
"""
from __future__ import annotations

import logging
import threading
import uuid

from backend.connectors.credentials import LAKEBASE_NAMESPACE, remember_effective
from backend.connectors.factory import build_connector
from backend.connectors.lakebase import LakebaseConnection
from backend.validation.comparator import merge_object_rescan, run_validation
from backend.validation.models import ValidationRunRequest, ValidationRunState

log = logging.getLogger("lakebase_express.validation.runs")

_RUNS: dict[str, ValidationRunState] = {}
_LOCK = threading.Lock()


def get_run(run_id: str) -> ValidationRunState | None:
    with _LOCK:
        state = _RUNS.get(run_id)
        # Return a copy so the caller never observes a half-mutated object.
        return state.model_copy(deep=True) if state else None


def start_run(req: ValidationRunRequest) -> str:
    run_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _RUNS[run_id] = ValidationRunState(run_id=run_id)
    threading.Thread(target=_execute, args=(run_id, req), daemon=True).start()
    return run_id


def _set(run_id: str, mutate) -> None:
    with _LOCK:
        state = _RUNS.get(run_id)
        if state:
            mutate(state)


def _execute(run_id: str, req: ValidationRunRequest) -> None:
    try:
        source = build_connector(
            req.source.source_type,
            host=req.source.host, database=req.source.database,
            username=req.source.username, password=req.source.password,
            port=req.source.port,
        )
        target = LakebaseConnection(**req.lakebase.conn_kwargs())

        def progress(phase: str, done: int, total: int, current: str) -> None:
            _set(run_id, lambda s: (
                setattr(s, "phase", phase),
                setattr(s, "tables_done", done),
                setattr(s, "tables_total", total),
                setattr(s, "current", current),
            ))

        validation_kwargs = {"scope": req.scope, "use_estimates": req.use_estimates}
        # Preserve the historical default call shape for wrappers around the
        # validation runner; the extra policy is needed only when explicitly set.
        if req.identifier_case.value == "preserve":
            validation_kwargs["identifier_case"] = req.identifier_case
        report = run_validation(
            source, target, req.target_schema, progress, **validation_kwargs
        )
        if req.scope == "objects" and req.previous:
            # Fold the fast object re-check into the earlier full report so the
            # table structure/row-count results survive.
            report = merge_object_rescan(req.previous, report)
        # The scan authenticated on both sides — keep the credential for the
        # session (mirrors the assessment and migration routes). A secret_ref
        # (stamped by the routes) persists the pointer instead of the value.
        remember_effective(req.source.source_type, req.source.host, req.source.database,
                          req.source.username, req.source.project_id,
                          req.source.password, req.source.secret_ref)
        remember_effective(LAKEBASE_NAMESPACE, req.lakebase.host, req.lakebase.database,
                          req.lakebase.user, req.lakebase.project_id,
                          req.lakebase.password, req.lakebase.secret_ref)
        _set(run_id, lambda s: (
            setattr(s, "status", "success"),
            setattr(s, "phase", "done"),
            setattr(s, "current", ""),
            setattr(s, "report", report),
        ))
    except Exception as exc:  # surfaced to the polling UI
        log.exception("Validation run %s failed", run_id)
        _set(run_id, lambda s: (setattr(s, "status", "failed"), setattr(s, "error", str(exc))))
