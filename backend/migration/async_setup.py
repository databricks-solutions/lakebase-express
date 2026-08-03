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

from backend.data_migration.models import DataGenRequest
from backend.migration.job_offload import create_job_and_run, create_scheduled_job

log = logging.getLogger("lakebase_express.async_setup")


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
    if quartz_cron:
        result = create_scheduled_job(req, workspace_dir, quartz_cron, timezone_id)
        result["note"] = (
            "Scheduled snapshot job created — it refreshes the Lakebase tables on the chosen "
            "interval. Apply the schema & code plan first so the target tables exist."
        )
        return result

    if not run_now:
        result = create_scheduled_job(req, workspace_dir, None, timezone_id)
        result["run_id"] = None
        result["run_url"] = None
        result["note"] = (
            "Snapshot job created but not started — open it in Databricks to pick the compute "
            "(serverless or a classic job cluster), tune it, and hit 'Run now'. Apply the "
            "schema & code plan first so the target tables exist."
        )
        return result

    result = create_job_and_run(req, workspace_dir)
    result["scheduled"] = False
    result["note"] = (
        "Snapshot job created and a run submitted — the copy task runs first, then one "
        "chained task per object type (constraints → indexes → foreign keys → triggers) "
        "creates them. Re-run it anytime from the Jobs UI ('Run now'). Apply the schema & "
        "code plan first so the target tables exist."
    )
    return result
