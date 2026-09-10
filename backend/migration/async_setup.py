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

# Baked into the notebooks at generation, so a job created without them never
# starts reporting, however often it is run later.
RUN_STATE_DISABLED = (
    "Run state will not be recorded: no Lakebase run store is reachable from a job. "
    "Either runs are not kept in Lakebase (LBX_PROJECTS_BACKEND=postgres), or this "
    "identity cannot list Lakebase projects to find the endpoint to authenticate "
    "against — check the app log, and set LBX_PROJECTS_PG_ENDPOINT to override the "
    "lookup. Then provision again: notebooks already uploaded cannot start reporting "
    "on their own."
)


def with_run_store(req: DataGenRequest) -> DataGenRequest:
    """Point the generated notebooks at the app's run store — server-side config,
    never from the client. No endpoint path means no OAuth, so no reporting."""
    config = get_run_store().notebook_config()
    if config is None:
        return req
    return req.model_copy(update={"run_store": RunStoreTarget(**config)})


# undefined_object — the grantee has no Postgres role, the one failure the user
# can fix themselves.
_MISSING_ROLE = "42704"


def create_role_snippet(identity: str, identity_type: str, endpoint: str) -> str:
    """What the user runs to give a job identity a Lakebase role. Needs a session
    authenticated as a Databricks identity (``databricks psql``); the app's password
    login is not one, which is why it cannot do this itself."""
    project = endpoint.split("/branches/")[0].removeprefix("projects/")
    # Doubled, or a quote in an identity would break the snippet.
    literal = identity.replace("'", "''")
    return (
        f"-- Run against the Lakebase project holding the run-state table:\n"
        f"--   databricks psql --project {project}\n"
        "CREATE EXTENSION IF NOT EXISTS databricks_auth;\n"
        f"SELECT databricks_create_role('{literal}', '{identity_type}');"
    )


def grant_run_store_access(job_id: int) -> tuple[str, str] | None:
    """Give the job's identity write access to the run-state table; (message, fix)
    when it could not be granted, else None.

    A job's OAuth credential needs a Lakebase role, which Lakebase creates only for
    the project owner — so a deployed app's service principal has none and the app
    cannot create it. Surfaced now, or it resurfaces as a password failure inside
    the job.
    """
    store = get_run_store()
    config = store.notebook_config()
    if config is None:
        return None
    identity = "unknown"
    try:
        identity, identity_type = run_as_identity(workspace_client(), job_id)
        store.grant_writer(identity)  # type: ignore[attr-defined]
        log.info("Granted run-store write access to %s", identity)
        return None
    except Exception as exc:
        log.warning("Could not grant run-store access to %s: %s", identity, exc)
        fix = create_role_snippet(identity, identity_type, config["endpoint"])
        if getattr(exc, "sqlstate", None) == _MISSING_ROLE:
            return (
                f"Run history will not record this job. It runs as {identity}, which has "
                "no Lakebase Postgres role to authenticate against — Lakebase creates one "
                "only for the project owner. Create it, then provision again. The "
                "migration itself is unaffected.",
                fix,
            )
        return (
            f"The job runs as {identity}, which could not be granted write access to the "
            f"run-state table ({exc}). The migration will still run; its progress just "
            "will not appear in run history. If that identity has no Lakebase role yet, "
            "this creates one.",
            fix,
        )


def check_run_store_access(job_id: int | None) -> tuple[str, str | None] | None:
    """(message, fix) when the job cannot record its run state, else None. Shared by
    provisioning and the user's re-check."""
    if get_run_store().notebook_config() is None:
        return RUN_STATE_DISABLED, None
    if job_id is None:
        return None
    return grant_run_store_access(job_id)


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
    failed = check_run_store_access(result.get("job_id"))
    if failed:
        result["run_state_warning"], result["run_state_fix"] = failed
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
