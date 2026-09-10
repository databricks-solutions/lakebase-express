"""Async (Databricks job) migrations, recorded in the run store.

Two kinds, because provisioning a job and running one are different events:

``async_job``  one row per provisioning — created, or scheduled. Never becomes
               running/success: the job may execute never, once, or nightly.
``async_run``  one execution, written from inside the generated notebooks, so runs
               triggered from the Jobs UI or by the schedule are recorded too.

A run this app triggered is written here under the id the notebooks derive, so
their first write updates our row instead of adding a second.
"""
from __future__ import annotations

import logging
import uuid

from backend.migration.models import AsyncRunState
from backend.run_registry import RunRegistry
from backend.run_store import run_state_id

log = logging.getLogger("lakebase_express.async_runs")

_JOBS: RunRegistry[AsyncRunState] = RunRegistry("async_job", AsyncRunState)
_RUNS: RunRegistry[AsyncRunState] = RunRegistry("async_run", AsyncRunState)


def get_run(run_id: str) -> AsyncRunState | None:
    return _RUNS.get(run_id) or _JOBS.get(run_id)


def record(
    result: dict,
    *,
    tables_total: int = 0,
    quartz_cron: str | None = None,
    project_id: str = "",
) -> AsyncRunState:
    """Record what provisioning produced. ``result`` is what job_offload returned."""
    job_id = result.get("job_id")
    job_run_id = result.get("run_id")
    triggered = bool(job_id and job_run_id)
    state = AsyncRunState(
        # A triggered run takes the id its notebooks derive; a provisioned job has
        # no run to derive one from.
        run_id=run_state_id(job_id, job_run_id) if triggered else str(uuid.uuid4()),
        status="submitted" if triggered else ("scheduled" if quartz_cron else "created"),
        job_id=job_id,
        job_run_id=job_run_id,
        job_url=result.get("url"),
        run_url=result.get("run_url"),
        notebook_path=result.get("notebook_path") or "",
        tables_total=tables_total,
        scheduled=bool(result.get("scheduled") or quartz_cron),
        quartz_cron=quartz_cron,
    )
    (_RUNS if triggered else _JOBS).create(state, project_id)
    return state
