"""Offload the data load to a Databricks Job (the 'hybrid' scale path).

Uploads the generated PySpark snapshot notebook to the workspace and provisions a
persistent job (optionally scheduled) that executes the loader — persistent even
for one-off runs, so the user can re-run the snapshot later from the Jobs UI with
'Run now'. Each migration project gets its own job and notebook folder, reused by
name across clicks (repointed at the freshly uploaded notebooks) instead of piling
up duplicates. Requires a live workspace with serverless job compute and is
intentionally isolated from the in-app loader so the tested core never depends on it.
"""
from __future__ import annotations

import itertools
import logging

from databricks.sdk.service import jobs, workspace

from backend.config import workspace_client
from backend.data_migration.etl_generator import generate, task_key
from backend.data_migration.models import DataGenRequest

log = logging.getLogger("lakebase_express.job_offload")

# Base name: prefixes every per-project job name, and stands alone when a request
# carries no project id.
JOB_NAME = "lakebase-express-snapshot"

# The id identifies the job durably; the name only makes the Jobs UI readable.
PROJECT_ID_TAG = "lbx_project_id"
PROJECT_NAME_TAG = "lbx_project"

# Bounded: the tag scan only runs when we would otherwise create a duplicate.
_TAG_SCAN_LIMIT = 500

# Cap on the project name in a job title.
_NAME_LIMIT = 60


def project_name(project_id: str) -> str:
    """The project's display name, best effort — decoration must not fail a
    provision."""
    if not project_id:
        return ""
    try:
        from backend.projects.store import get_store

        project = get_store().get(project_id)
        return (project.name if project else "") or ""
    except Exception as exc:
        log.warning("Could not resolve the name of project %s: %s", project_id, exc)
        return ""


def job_name(project_id: str, name: str | None = None) -> str:
    """One job per migration project, titled by the project so it is recognisable.
    Titles are not unique; the id tag is what identifies it (see _find_job)."""
    if not project_id:
        return JOB_NAME
    label = (name if name is not None else project_name(project_id)).strip()
    label = " ".join(label.split())[:_NAME_LIMIT]
    # No name: the id, rather than a title shared by every unnameable project.
    return f"{JOB_NAME} · {label}" if label else f"{JOB_NAME} ({project_id})"


def _tag_value(value: str) -> str:
    """Tags reach the cloud provider, which rejects characters Databricks allows in
    a project name."""
    kept = "".join(c if (c.isalnum() or c in " +-=._:/@") else "-" for c in value)
    return " ".join(kept.split())[:255]


def job_tags(project_id: str, name: str | None = None) -> dict[str, str]:
    """Lets the Jobs UI filter by project, and survives either being renamed."""
    if not project_id:
        return {}
    tags = {PROJECT_ID_TAG: _tag_value(project_id)}
    label = _tag_value(name if name is not None else project_name(project_id))
    if label:
        tags[PROJECT_NAME_TAG] = label
    return tags


def project_dir(workspace_dir: str, project_id: str) -> str:
    """Per-project notebook folder — separate jobs sharing one folder would both run
    whichever load was uploaded last."""
    base = workspace_dir.rstrip("/")
    return f"{base}/{project_id}" if project_id else base


def upload_notebooks(w, req: DataGenRequest, workspace_dir: str) -> list[str]:
    """Upload generated notebooks to the workspace; return all paths in order.

    The first path is the loader (PySpark snapshot); the rest are the per-type
    post-load DDL notebooks (constraints, indexes, foreign keys, triggers).
    """
    base = project_dir(workspace_dir, req.project_id)
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


def _project_of(job) -> str | None:
    """The project a job is tagged for, or None if it carries no such tag."""
    return ((job.settings.tags if job.settings else None) or {}).get(PROJECT_ID_TAG)


def _find_job(w, name: str, project_id: str):
    """The job we already manage for this project, if any.

    A same-named job is only ours if it is not tagged for another project, or two
    projects sharing a name would repoint each other's. The tag scan then catches a
    job whose project (or title) was renamed.
    """
    untagged = None
    for job in w.jobs.list(name=name):
        owner = _project_of(job)
        if project_id and owner == project_id:
            return job
        if owner is None and untagged is None:
            untagged = job
    if untagged is not None:
        return untagged
    if not project_id:
        return None
    try:
        for job in itertools.islice(w.jobs.list(), _TAG_SCAN_LIMIT):
            if _project_of(job) == project_id:
                log.info("Reusing job %s for project %s (matched by tag)", job.job_id, project_id)
                return job
    except Exception as exc:
        # Worst case we create a second job — exactly what we'd have done anyway.
        log.warning("Could not search jobs by tag: %s", exc)
    return None


def _ensure_job(w, paths: list[str], schedule: jobs.CronSchedule | None, name: str,
                tags: dict[str, str], project_id: str = "") -> tuple[int, bool]:
    """Reuse this project's job (see _find_job) or create it; (job_id, created).

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
        key = task_key(path.rsplit("/", 1)[-1])
        tasks.append(
            jobs.Task(
                task_key=key,
                notebook_task=jobs.NotebookTask(
                    notebook_path=path,
                    # Dynamic values, resolved per run: every task derives the same
                    # run-state id from them (see etl_generator._RUN_STATE).
                    base_parameters={"job_id": "{{job.id}}", "job_run_id": "{{job.run_id}}"},
                ),
                depends_on=[jobs.TaskDependency(task_key=prev_key)] if prev_key else None,
            )
        )
        prev_key = key
    existing = _find_job(w, name, project_id)
    if existing:
        w.jobs.reset(
            job_id=existing.job_id,
            new_settings=jobs.JobSettings(name=name, tasks=tasks, schedule=schedule, tags=tags),
        )
        return existing.job_id, False
    return w.jobs.create(name=name, tasks=tasks, schedule=schedule, tags=tags).job_id, True


def create_job_and_run(req: DataGenRequest, workspace_dir: str) -> dict:
    """Provision the (unscheduled) persistent snapshot job and trigger a run now.

    A persistent job rather than a one-time submit, so the snapshot can be re-run
    later from the Jobs UI with 'Run now'.
    """
    w = workspace_client()
    paths = upload_notebooks(w, req, workspace_dir)
    label = project_name(req.project_id)
    job_id, created = _ensure_job(
        w, paths, None, job_name(req.project_id, label),
        job_tags(req.project_id, label), req.project_id,
    )
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
    label = project_name(req.project_id)
    job_id, created = _ensure_job(
        w, paths, schedule, job_name(req.project_id, label),
        job_tags(req.project_id, label), req.project_id,
    )
    host = (w.config.host or "").rstrip("/")
    return {
        "job_id": job_id,
        "job_created": created,
        "url": f"{host}/jobs/{job_id}" if host else None,
        "notebook_path": paths[0],
        "notebook_paths": paths,
        "scheduled": bool(quartz_cron),
    }


def run_as_identity(w, job_id: int) -> tuple[str, str]:
    """(name, identity_type) of the identity a job's tasks run as — who needs a
    Lakebase role and write access. Falls back to the caller, as run_as does."""
    try:
        job = w.jobs.get(job_id=job_id)
        run_as = getattr(getattr(job, "settings", None), "run_as", None)
        if run_as is not None:
            if getattr(run_as, "service_principal_name", None):
                return run_as.service_principal_name, "SERVICE_PRINCIPAL"
            if getattr(run_as, "user_name", None):
                return run_as.user_name, "USER"
        name = getattr(job, "run_as_user_name", None)
        if name:
            return name, _identity_type(name)
    except Exception:
        pass
    name = w.current_user.me().user_name
    return name, _identity_type(name)


def _identity_type(name: str) -> str:
    """Only for a job with no structured run_as: user names are emails, a service
    principal's is its application id."""
    return "USER" if "@" in name else "SERVICE_PRINCIPAL"


def job_status(run_id: int) -> dict:
    w = workspace_client()
    r = w.jobs.get_run(run_id)
    state = r.state
    return {
        "life_cycle_state": str(getattr(state, "life_cycle_state", "")),
        "result_state": str(state.result_state) if state and state.result_state else None,
        "url": r.run_page_url,
    }
