"""In-memory registry of plan-build runs + background execution.

Plan generation translates every programmable object through a Foundation Model.
For a real schema that's minutes of work — well past the Databricks Apps ~120s
front-proxy request timeout, which silently drops the connection mid-request. So
the build runs on a daemon thread (same pattern as the data-load and validation
phases) and the UI polls ``PlanRunState`` for progress and the finished plan.

Translations run concurrently inside build_plan; state lives in process memory,
fine for a single-user accelerator App.
"""
from __future__ import annotations

import logging
import threading
import uuid

from backend.migration.models import BuildPlanRequest, PlanRunState
from backend.migration.planner import build_plan

log = logging.getLogger("lakebase_express.plan_runs")

_RUNS: dict[str, PlanRunState] = {}
_LOCK = threading.Lock()


def get_run(run_id: str) -> PlanRunState | None:
    with _LOCK:
        state = _RUNS.get(run_id)
        # Return a copy so the caller never observes a half-mutated object.
        return state.model_copy(deep=True) if state else None


def start_run(req: BuildPlanRequest) -> str:
    run_id = uuid.uuid4().hex[:12]
    state = PlanRunState(
        run_id=run_id,
        status="running",
        objects_total=len(req.programmable_objects) if req.translate else 0,
    )
    with _LOCK:
        _RUNS[run_id] = state
    threading.Thread(target=_execute, args=(run_id, req), daemon=True).start()
    return run_id


def _set(run_id: str, mutate) -> None:
    with _LOCK:
        state = _RUNS.get(run_id)
        if state:
            mutate(state)


def _execute(run_id: str, req: BuildPlanRequest) -> None:
    try:
        def progress(done: int, total: int) -> None:
            _set(run_id, lambda s: (
                setattr(s, "objects_done", done),
                setattr(s, "objects_total", total),
            ))

        items = build_plan(
            req.tables, req.programmable_objects, req.target_schema,
            req.translate, req.endpoint, on_translate_progress=progress,
            identifier_case=req.identifier_case,
        )
        _set(run_id, lambda s: (
            setattr(s, "status", "success"),
            setattr(s, "items", items),
        ))
    except Exception as exc:  # surfaced to the polling UI
        log.exception("Plan build %s failed", run_id)
        _set(run_id, lambda s: (setattr(s, "status", "failed"), setattr(s, "error", str(exc))))
