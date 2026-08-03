"""Offload the data load to a Databricks Job (the 'hybrid' scale path).

Uploads the generated PySpark snapshot notebook to the workspace and provisions a
persistent job (optionally scheduled) that executes the loader — persistent even
for one-off runs, so the user can re-run the snapshot later from the Jobs UI with
'Run now'. The job is reused by name across clicks (repointed at the freshly
uploaded notebook) instead of piling up duplicates. Requires a live workspace with
serverless job compute and is intentionally isolated from the in-app loader so the
tested core never depends on it.
"""
from __future__ import annotations

import logging

from databricks.sdk.service import jobs, workspace

from backend.config import workspace_client
from backend.data_migration.etl_generator import generate
from backend.data_migration.models import DataGenRequest

log = logging.getLogger("lakebase_express.job_offload")

JOB_NAME = "lakebase-express-snapshot"


def _task_key(filename_stem: str) -> str:
    """Job task key derived from the notebook filename stem, minus its numeric
    prefix: 02_post_load_constraints -> post_load_constraints. The snapshot
    loader keeps the stable key "load"."""
    parts = filename_stem.split("_", 1)
    stem = parts[1] if len(parts) > 1 and parts[0].isdigit() else filename_stem
    return "load" if stem == "snapshot_load" else stem


def upload_notebooks(w, req: DataGenRequest, workspace_dir: str) -> list[str]:
    """Upload generated notebooks to the workspace; return all paths in order.

    The first path is the loader (PySpark snapshot); the rest are the per-type
    post-load DDL notebooks (constraints, indexes, foreign keys, triggers).
    """
    base = workspace_dir.rstrip("/")
    w.workspace.mkdirs(base)
    paths: list[str] = []
    for art in generate(req):
        path = f"{base}/{art.filename.removesuffix('.py')}"
        # Delete first: overwriting only replaces the source, not notebook sidecar
        # metadata (e.g. the serverless environment panel). A stale environment
        # entry is installed before any cell runs and can fail the whole job.
        try:
            w.workspace.delete(path)
        except Exception:
            pass  # didn't exist yet
        w.workspace.upload(
            path,
            art.code.encode("utf-8"),
            format=workspace.ImportFormat.SOURCE,
            language=workspace.Language.PYTHON,
            overwrite=True,
        )
        paths.append(path)
    # The job is fully reset from `paths` on every setup, so a post-load type
    # that no longer has statements simply isn't a task anymore; any leftover
    # notebook file from a prior run is orphaned but harmless (never referenced).
    return paths


def _ensure_job(w, paths: list[str], schedule: jobs.CronSchedule | None) -> tuple[int, bool]:
    """Reuse the accelerator's job (matched by name) or create it; (job_id, created).

    The job is a linear chain of tasks: the PySpark snapshot copy, then one task
    per post-load object type (constraints → indexes → foreign keys → triggers),
    each depending on the previous so it only runs once the earlier phase
    succeeds. Splitting per type keeps the job graph legible — a failure shows
    exactly which phase broke — and each task is independently re-runnable via
    'Repair run'. An existing job is repointed at the freshly uploaded notebooks
    and its schedule replaced, so repeated setups manage one job, not duplicates.
    """
    tasks: list[jobs.Task] = []
    prev_key: str | None = None
    for path in paths:
        key = _task_key(path.rsplit("/", 1)[-1])
        tasks.append(
            jobs.Task(
                task_key=key,
                notebook_task=jobs.NotebookTask(notebook_path=path),
                depends_on=[jobs.TaskDependency(task_key=prev_key)] if prev_key else None,
            )
        )
        prev_key = key
    existing = next(iter(w.jobs.list(name=JOB_NAME)), None)
    if existing:
        w.jobs.reset(
            job_id=existing.job_id,
            new_settings=jobs.JobSettings(name=JOB_NAME, tasks=tasks, schedule=schedule),
        )
        return existing.job_id, False
    return w.jobs.create(name=JOB_NAME, tasks=tasks, schedule=schedule).job_id, True


def create_job_and_run(req: DataGenRequest, workspace_dir: str) -> dict:
    """Provision the (unscheduled) persistent snapshot job and trigger a run now.

    A persistent job rather than a one-time submit, so the snapshot can be re-run
    later from the Jobs UI with 'Run now'.
    """
    w = workspace_client()
    paths = upload_notebooks(w, req, workspace_dir)
    job_id, created = _ensure_job(w, paths, schedule=None)
    run = w.jobs.run_now(job_id=job_id)
    run_id = getattr(run, "run_id", None) or run.response.run_id
    host = (w.config.host or "").rstrip("/")
    return {
        "job_id": job_id,
        "job_created": created,
        "run_id": run_id,
        "notebook_path": paths[0],
        "notebook_paths": paths,
        "url": f"{host}/jobs/{job_id}" if host else None,
        "run_url": f"{host}/jobs/runs/{run_id}" if host else None,
    }


def create_scheduled_job(
    req: DataGenRequest,
    workspace_dir: str,
    quartz_cron: str | None = None,
    timezone_id: str = "UTC",
) -> dict:
    """Create a persistent Databricks (Lakeflow) Job for the migration.

    When ``quartz_cron`` is given the job runs on that schedule; otherwise it is
    created unscheduled (run it manually from the Jobs UI / 'Run now').
    """
    w = workspace_client()
    paths = upload_notebooks(w, req, workspace_dir)

    schedule = (
        jobs.CronSchedule(
            quartz_cron_expression=quartz_cron,
            timezone_id=timezone_id or "UTC",
            pause_status=jobs.PauseStatus.UNPAUSED,
        )
        if quartz_cron
        else None
    )
    job_id, created = _ensure_job(w, paths, schedule)
    host = (w.config.host or "").rstrip("/")
    return {
        "job_id": job_id,
        "job_created": created,
        "url": f"{host}/jobs/{job_id}" if host else None,
        "notebook_path": paths[0],
        "notebook_paths": paths,
        "scheduled": bool(quartz_cron),
    }


def job_status(run_id: int) -> dict:
    w = workspace_client()
    r = w.jobs.get_run(run_id)
    state = r.state
    return {
        "life_cycle_state": str(getattr(state, "life_cycle_state", "")),
        "result_state": str(state.result_state) if state and state.result_state else None,
        "url": r.run_page_url,
    }
