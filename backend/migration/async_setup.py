"""Provision async mode: a PySpark **snapshot** job into Lakebase.

Async mode does a one-off full copy of the selected tables from the source into the
Lakebase Postgres tables the schema & code migration plan created (the same plan
Sync mode applies). The copy runs as a Databricks Job so it can scale out beyond
the in-app loader — submitted once, or created on a recurring schedule to refresh
the snapshot. There is no CDC, Delta landing or synced table.

Like ``job_offload``, this is isolated from the in-app loader so the tested core
never depends on a live workspace.
"""
from __future__ import annotations

import logging

from backend.config import workspace_client
from backend.data_migration.models import DataGenRequest, RunStoreTarget
from backend.migration import async_runs
from backend.migration.job_offload import (
    create_job_and_run,
    create_scheduled_job,
    run_as_identity,
)
from backend.run_store import get_run_store

log = logging.getLogger("lakebase_express.async_setup")

# The job's coordinates are baked into the notebooks when they are generated, so a
# job created without them keeps reporting nothing however often it is run later.
RUN_STATE_DISABLED = (
    "Run state will not be recorded: no Lakebase run store is reachable from a job. "
    "Either runs are not kept in Lakebase (LBX_PROJECTS_BACKEND=postgres), or this "
    "identity cannot list Lakebase projects to find the endpoint to authenticate "
    "against — check the app log, and set LBX_PROJECTS_PG_ENDPOINT to override the "
    "lookup. Then provision again: notebooks already uploaded cannot start reporting "
    "on their own."
)


def with_run_store(req: DataGenRequest) -> DataGenRequest:
    """Point the generated notebooks at the app's run store, so the job records
    its own state. Server-side config, never supplied by the client. A store with
    no Lakebase endpoint path can't be written to over OAuth, so it is skipped and
    the notebooks are generated without reporting."""
    config = get_run_store().notebook_config()
    if config is None:
        return req
    return req.model_copy(update={"run_store": RunStoreTarget(**config)})


def grant_run_store_access(job_id: int) -> str | None:
    """Let the job's identity write run state. Returns a message when it could not
    be granted — surfaced to the caller, because the alternative is discovering it
    later as a missing history row."""
    store = get_run_store()
    if store.notebook_config() is None:
        return None
    identity = "unknown"
    try:
        identity = run_as_identity(workspace_client(), job_id)
        store.grant_writer(identity)  # type: ignore[attr-defined]
        log.info("Granted run-store write access to %s", identity)
        return None
    except Exception as exc:
        log.warning("Could not grant run-store access to %s: %s", identity, exc)
        return (
            f"The job runs as {identity}, which could not be granted write access to the "
            f"run-state table ({exc}). The migration will still run; its progress just "
            "will not appear in run history until that identity can write to the table."
        )


def setup_async(
    req: DataGenRequest,
    workspace_dir: str,
    quartz_cron: str | None = None,
    timezone_id: str = "UTC",
    run_now: bool = True,
) -> dict:
    """Provision the PySpark snapshot job — run now, create only, or on a schedule.

    All paths create/reuse a persistent Databricks Job, so the snapshot can be
    re-run later from the Jobs UI with 'Run now'; the one-off path also triggers
    an immediate run, while ``run_now=False`` leaves the job unstarted so the
    user can pick/tune the compute (serverless or a classic job cluster) in the
    Jobs UI before running it. Either way, apply the schema & code plan first so
    the target Lakebase tables exist before the snapshot runs.
    """
    req = with_run_store(req)
    if quartz_cron:
        result = create_scheduled_job(req, workspace_dir, quartz_cron, timezone_id)
        result["note"] = (
            "Scheduled snapshot job created — it refreshes the Lakebase tables on the chosen "
            "interval. Apply the schema & code plan first so the target tables exist."
        )
        return _recorded(result, req, quartz_cron)

    if not run_now:
        result = create_scheduled_job(req, workspace_dir, None, timezone_id)
        result["run_id"] = None
        result["run_url"] = None
        result["note"] = (
            "Snapshot job created but not started — open it in Databricks to pick the compute "
            "(serverless or a classic job cluster), tune it, and hit 'Run now'. Apply the "
            "schema & code plan first so the target tables exist."
        )
        return _recorded(result, req, None)

    result = create_job_and_run(req, workspace_dir)
    result["scheduled"] = False
    result["note"] = (
        "Snapshot job created and a run submitted — the copy task runs first, then one "
        "chained task per object type (constraints → indexes → foreign keys → triggers) "
        "creates them. Re-run it anytime from the Jobs UI ('Run now'). Apply the schema & "
        "code plan first so the target tables exist."
    )
    return _recorded(result, req, None)


def _recorded(result: dict, req: DataGenRequest, quartz_cron: str | None) -> dict:
    """Record the provisioned job in the run store and return ``result`` with our
    own run id added. Recording must never fail the provisioning that succeeded."""
    job_id = result.get("job_id")
    if get_run_store().notebook_config() is None:
        result["run_state_warning"] = RUN_STATE_DISABLED
    elif job_id:
        warning = grant_run_store_access(job_id)
        if warning:
            result["run_state_warning"] = warning
    try:
        state = async_runs.record(
            result,
            tables_total=len(req.tables),
            quartz_cron=quartz_cron,
            project_id=req.project_id,
        )
        result["lbx_run_id"] = state.run_id
    except Exception as exc:
        log.warning("Could not record the async run: %s", exc)
    return result
