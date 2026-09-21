"""Async (Databricks job) migrations, recorded in the run store.

Two kinds, because provisioning a job and running one are different events:

``async_job``  one row per provisioning — created, or scheduled. Never becomes
               running/success: the job may execute never, once, or nightly.
``async_run``  one execution, written from inside the generated notebooks, so runs
               triggered from the Jobs UI or by the schedule are recorded too.

A run this app triggered is written here under the id the notebooks derive, so
their first write updates our row instead of adding a second.

The loader notebook also checkpoints each table as it settles, which is what lets a
failed run be resumed — only the tables it did not finish are copied again.
"""
from __future__ import annotations

import logging
import uuid
from typing import Callable

from backend.migration.models import AsyncRunState
from backend.run_registry import RunRegistry
from backend.run_store import run_state_id

log = logging.getLogger("lakebase_express.async_runs")

_JOBS: RunRegistry[AsyncRunState] = RunRegistry("async_job", AsyncRunState)
_RUNS: RunRegistry[AsyncRunState] = RunRegistry("async_run", AsyncRunState)

# Statuses that mean a table needs no further work.
_DONE = frozenset({"success", "skipped"})
# What the notebooks write once they are done with the run itself.
_TERMINAL = frozenset({"success", "failed"})


def get_run(run_id: str) -> AsyncRunState | None:
    return _RUNS.get(run_id) or _JOBS.get(run_id)


# --- Resume ----------------------------------------------------------------------


def tables_left(state: AsyncRunState) -> int:
    """Tables a resume would still copy. ``tables_total`` is what provisioning
    recorded; a run known only from its own notebook has none, so its checkpoints
    are all there is to go on."""
    loaded = sum(t.status in _DONE for t in state.tables.values())
    if state.tables_total:
        return max(state.tables_total - loaded, 0)
    return sum(t.status not in _DONE for t in state.tables.values())


def is_resumable(state: AsyncRunState, job_active: bool | None = None) -> bool:
    """True when the job run is over and a resume would skip work it already did.

    ``job_active`` is what the Jobs API says about the Databricks run. A run still
    marked running counts as over only once the workspace agrees it has finished:
    its tasks record nothing while a table is mid-COPY, and a second job loading the
    same table would fight over the same TRUNCATE.
    """
    over = state.status == "failed" or (state.status not in _TERMINAL and job_active is False)
    if not over or tables_left(state) <= 0:
        return False
    # Nothing loaded and no FKs to put back: resuming would be the same full copy.
    return any(t.status in _DONE for t in state.tables.values()) or bool(state.dropped_fks)


def find_resumable(
    project_id: str, limit: int = 20, job_active: Callable[[int], bool | None] | None = None
) -> AsyncRunState | None:
    """The project's newest async run worth resuming, for the UI's resume offer.

    ``job_active`` is asked about a run still marked running (see is_resumable). A run
    that a later one already resumed is never offered again: whatever it left behind
    is that resume's business now, which is why this walks newest-first.
    """
    superseded: set[str] = set()
    for record in _RUNS.list(limit, project_id):
        state = _RUNS.get(record.run_id)
        if state is None:
            continue
        if state.resumed_from:
            superseded.add(state.resumed_from)
        if state.run_id in superseded:
            continue
        active = None
        if state.status not in _TERMINAL and job_active and state.job_run_id:
            active = job_active(state.job_run_id)
        if is_resumable(state, active):
            return state
    return None


def belongs_to(run_id: str, project_id: str, limit: int = 50) -> bool:
    """Whether this project's recent async runs include that run. The run row carries
    no project id of its own, so the store's index is what links the two."""
    if not project_id:
        return True
    return any(record.run_id == run_id for record in _RUNS.list(limit, project_id))


# --- Recording -------------------------------------------------------------------


def record(
    result: dict,
    *,
    tables_total: int = 0,
    quartz_cron: str | None = None,
    project_id: str = "",
    resume_from: str | None = None,
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
        # Only a triggered run resumes one; the notebook records this too, for a
        # resume started from the Jobs UI.
        resumed_from=resume_from if triggered else None,
    )
    (_RUNS if triggered else _JOBS).create(state, project_id)
    return state
