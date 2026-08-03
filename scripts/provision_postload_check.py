"""One-off: provision a POST-LOAD-ONLY job against an already-loaded Lakebase.

Applies just the migration plan's post-data phase (constraints, indexes, foreign
keys, triggers) as its own job, to surface inconsistencies before re-running a
full migration. Creates a NEW job; the lakebase-express-snapshot job is untouched.

Constraints/indexes/FKs are rebuilt from the project's stored assessment rather
than its plan, so they're correct even if the UI's saved plan is stale; triggers
come from the plan, already AI-translated.

Config comes from the environment so nothing workspace-specific is committed:

    export LBX_PROFILE="<cli-profile>"
    export LBX_APP_URL="$(databricks apps get lakebase-express -p "$LBX_PROFILE" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["url"])')"
    export LBX_APP_TOKEN="$(databricks auth token --profile "$LBX_PROFILE" | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')"
    export LBX_PROJECT_ID="<project-id>"          # from the app's project list
    export LBX_PG_HOST="<instance>.database.<region>.azuredatabricks.net"
    export LBX_PG_DATABASE="<db>"                 # the already-loaded target DB
    export LBX_PG_USER="<lakebase-role>"
    PYTHONPATH=. python3 scripts/provision_postload_check.py
"""
from __future__ import annotations

import json
import os
import urllib.request

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import jobs, workspace

from backend.assessment.models import TableInfo
from backend.data_migration.etl_generator import post_load_artifacts
from backend.data_migration.models import DataGenRequest, PostLoadStatement, TableRef
from backend.migration.job_offload import _task_key
from backend.migration.models import ObjectKind
from backend.migration.planner import build_plan


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set — see this script's docstring for the full list.")
    return value


PROFILE = os.getenv("LBX_PROFILE") or None  # None => default SDK auth chain
PROJECT_ID = _required("LBX_PROJECT_ID")
APP_URL = _required("LBX_APP_URL").rstrip("/")
APP_TOKEN = _required("LBX_APP_TOKEN")

WORKSPACE_DIR = os.getenv("LBX_WORKSPACE_DIR", "/Workspace/Shared/lakebase-express-postload-check")
JOB_NAME = os.getenv("LBX_JOB_NAME", "lakebase-express-postload-check")
TARGET_SCHEMA = os.getenv("LBX_TARGET_SCHEMA", "public")

# Password is read from the secret scope at runtime, never passed in.
PG = dict(
    lakebase_host=_required("LBX_PG_HOST"),
    lakebase_port=int(os.getenv("LBX_PG_PORT", "5432")),
    lakebase_database=_required("LBX_PG_DATABASE"),
    lakebase_user=_required("LBX_PG_USER"),
    lakebase_password_secret_key=os.getenv("LBX_PG_SECRET_KEY", "lakebase-password"),
    secret_scope=os.getenv("LBX_SECRET_SCOPE", "lakebase-express"),
)

# Post-data kinds we rebuild deterministically from the assessment (no AI).
_DETERMINISTIC = {ObjectKind.CONSTRAINT, ObjectKind.INDEX, ObjectKind.FOREIGN_KEY}


def _fetch_project() -> dict:
    req = urllib.request.Request(
        f"{APP_URL}/api/projects/{PROJECT_ID}",
        headers={"Authorization": f"Bearer {APP_TOKEN}"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))


def _post_load_statements(project: dict) -> list[PostLoadStatement]:
    """Combine deterministic constraint/index/FK DDL (from the assessment) with
    the plan's already-translated triggers, in dependency-safe order."""
    asmt = project.get("assessment") or {}
    tables = [TableInfo(**t) for t in asmt.get("tables", [])]

    # Constraints -> indexes -> foreign keys, generated straight from the scan.
    built = build_plan(tables, [], target_schema=TARGET_SCHEMA, translate=False, endpoint=None)
    stmts = [
        PostLoadStatement(name=i.name, sql=i.sql, kind=i.kind.value)
        for i in built
        if i.kind in _DETERMINISTIC and i.sql.strip()
    ]

    # Triggers from the persisted plan (already AI-translated); sanitized at emit
    # time by the notebook generator.
    stmts += [
        PostLoadStatement(name=i["name"], sql=i["sql"], kind="trigger")
        for i in (project.get("plan") or [])
        if i["kind"] == "trigger" and (i.get("sql") or "").strip()
    ]
    return stmts


def main() -> None:
    project = _fetch_project()
    statements = _post_load_statements(project)

    by_kind: dict[str, int] = {}
    for s in statements:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    print(f"Post-load statements assembled: {len(statements)}")
    for k in ("constraint", "index", "foreign_key", "trigger"):
        if by_kind.get(k):
            print(f"  {k}: {by_kind[k]}")

    req = DataGenRequest(
        host="unused", database="unused", username="unused", password_secret_key="unused",
        tables=[TableRef(schema_name="_", table_name="_")],
        target_schema=TARGET_SCHEMA,
        post_load_sql=statements,
        **PG,
    )
    arts = post_load_artifacts(req)
    if not arts:
        raise SystemExit("No post-load notebooks to generate.")
    print("\nGenerating notebooks:", [a.filename for a in arts])

    w = WorkspaceClient(profile=PROFILE)
    base = WORKSPACE_DIR.rstrip("/")
    w.workspace.mkdirs(base)
    paths: list[str] = []
    for art in arts:
        path = f"{base}/{art.filename.removesuffix('.py')}"
        try:
            w.workspace.delete(path)
        except Exception:
            pass
        w.workspace.upload(
            path, art.code.encode("utf-8"),
            format=workspace.ImportFormat.SOURCE, language=workspace.Language.PYTHON,
            overwrite=True,
        )
        paths.append(path)
        print("  uploaded", path)

    tasks: list[jobs.Task] = []
    prev: str | None = None
    for path in paths:
        key = _task_key(path.rsplit("/", 1)[-1])
        tasks.append(jobs.Task(
            task_key=key,
            notebook_task=jobs.NotebookTask(notebook_path=path),
            depends_on=[jobs.TaskDependency(task_key=prev)] if prev else None,
        ))
        prev = key

    existing = next(iter(w.jobs.list(name=JOB_NAME)), None)
    if existing:
        w.jobs.reset(job_id=existing.job_id,
                     new_settings=jobs.JobSettings(name=JOB_NAME, tasks=tasks))
        job_id = existing.job_id
        print(f"\nReset existing job {job_id}")
    else:
        job_id = w.jobs.create(name=JOB_NAME, tasks=tasks).job_id
        print(f"\nCreated job {job_id}")

    host = (w.config.host or "").rstrip("/")
    print(f"Tasks: {[t.task_key for t in tasks]}")
    print(f"Job URL: {host}/jobs/{job_id}")
    print(f"\nRun:  databricks jobs run-now {job_id}" + (f" --profile {PROFILE}" if PROFILE else ""))


if __name__ == "__main__":
    main()
