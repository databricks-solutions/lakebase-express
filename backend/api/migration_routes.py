"""Execution endpoints: build plan, apply schema/code, load data, offload to job."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.assessment.models import ConnectionRequest
from backend.connectors.credentials import (
    LAKEBASE_NAMESPACE,
    remember_effective,
    resolve_effective_password,
)
from backend.connectors.lakebase import LakebaseConnection
from backend.data_migration.models import DataGenRequest
from backend.migration import async_setup, job_offload, plan_runs, runs, secret_setup
from backend.migration.executor import apply_plan
from backend.migration.models import (
    ApplyRequest,
    ApplyResponse,
    BuildPlanRequest,
    DataLoadRequest,
    ItemStatus,
    LakebaseConnRequest,
    PlanResponse,
    PlanRunState,
    RunState,
)
from backend.migration.planner import build_plan

log = logging.getLogger("lakebase_express.migration")

router = APIRouter(prefix="/api/migration", tags=["migration"])


class TestResult(BaseModel):
    ok: bool
    message: str


# --- Target connection -----------------------------------------------------------


def with_lakebase_password(lb: LakebaseConnRequest) -> LakebaseConnRequest:
    """Resolve the target password and return a request with it filled in.

    Precedence mirrors the source side: typed password → request secret_ref →
    stored secret_ref → cached plaintext. The SPA holds passwords per browser
    session, so a page reload sends them empty. The effective secret reference (if
    any) is stamped onto the returned model's ``secret_ref`` so ``_remember_lakebase``
    persists the pointer, not the resolved value. Shared with the validation routes.
    """
    password, ref = resolve_effective_password(
        LAKEBASE_NAMESPACE, lb.host, lb.database, lb.user, lb.project_id,
        lb.password, lb.secret_ref,
    )
    if not password:
        raise HTTPException(
            status_code=400,
            detail="No Lakebase password supplied and none cached from a previous "
                   "successful connection — re-enter it on Connections & Target.",
        )
    return lb.model_copy(update={"password": password, "secret_ref": ref})


def _remember_lakebase(lb: LakebaseConnRequest) -> None:
    remember_effective(LAKEBASE_NAMESPACE, lb.host, lb.database, lb.user, lb.project_id,
                       lb.password, lb.secret_ref)


@router.post("/lakebase/test", response_model=TestResult)
def test_lakebase(req: LakebaseConnRequest) -> TestResult:
    try:
        req = with_lakebase_password(req)
        ok = LakebaseConnection(**req.conn_kwargs()).test_connection()
        if ok:
            _remember_lakebase(req)
        return TestResult(ok=ok, message="Connected to Lakebase." if ok else "Probe failed.")
    except HTTPException as exc:  # missing password — keep the friendly TestResult shape
        return TestResult(ok=False, message=str(exc.detail))
    except Exception as exc:
        log.warning("Lakebase test failed: %s", exc)
        return TestResult(ok=False, message=str(exc))


# --- Plan + apply (schema & code) ------------------------------------------------


@router.post("/plan", response_model=PlanResponse)
def plan(req: BuildPlanRequest) -> PlanResponse:
    """Synchronous plan build. Fine for a schema with few/no code objects; for a
    real schema use /plan/start — AI translation runs past the Apps request
    timeout. Kept for scripts and small plans."""
    items = build_plan(
        req.tables,
        req.programmable_objects,
        req.target_schema,
        req.translate,
        req.endpoint,
        identifier_case=req.identifier_case,
    )
    return PlanResponse(items=items)


class StartPlanResponse(BaseModel):
    run_id: str


@router.post("/plan/start", response_model=StartPlanResponse)
def start_plan(req: BuildPlanRequest) -> StartPlanResponse:
    """Build the plan on a background thread and return a run_id to poll. AI
    translation of code objects runs well past the Databricks Apps ~120s request
    timeout, so it can't be done inline on the request."""
    return StartPlanResponse(run_id=plan_runs.start_run(req))


@router.get("/plan/status/{run_id}", response_model=PlanRunState)
def plan_status(run_id: str) -> PlanRunState:
    state = plan_runs.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown plan run id.")
    return state


@router.post("/apply", response_model=ApplyResponse)
def apply(req: ApplyRequest) -> ApplyResponse:
    lakebase = with_lakebase_password(req.lakebase)
    conn = LakebaseConnection(**lakebase.conn_kwargs())
    results = apply_plan(conn, req.items, req.stop_on_error)
    # apply_plan connects before applying, so returning at all means the
    # credentials authenticated — keep them for the session.
    _remember_lakebase(lakebase)
    return ApplyResponse(
        results=results,
        success=sum(r.status is ItemStatus.SUCCESS for r in results),
        failed=sum(r.status is ItemStatus.FAILED for r in results),
        skipped=sum(r.status is ItemStatus.SKIPPED for r in results),
    )


# --- Data load (in-app) ----------------------------------------------------------


class StartRunResponse(BaseModel):
    run_id: str


@router.post("/data/start", response_model=StartRunResponse)
def start_data(req: DataLoadRequest) -> StartRunResponse:
    # Resolve both sides before handing off to the background run: the source by
    # the shared precedence (typed → request/stored secret_ref → cached), the
    # target through with_lakebase_password.
    source_password, _ = resolve_effective_password(
        req.source_type, req.host, req.database, req.username, req.project_id,
        req.password, req.secret_ref,
    )
    if not source_password:
        raise HTTPException(
            status_code=400,
            detail="No source password supplied and none cached from a previous "
                   "successful connection — re-enter it on Connections & Target.",
        )
    req = req.model_copy(
        update={"password": source_password, "lakebase": with_lakebase_password(req.lakebase)}
    )
    return StartRunResponse(run_id=runs.start_run(req))


@router.get("/data/status/{run_id}", response_model=RunState)
def data_status(run_id: str) -> RunState:
    state = runs.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown run id.")
    return state


# --- Data load (offload to Databricks Job) ---------------------------------------


class OffloadRequest(BaseModel):
    spec: DataGenRequest
    workspace_dir: str = "/Workspace/Shared/lakebase-express"


class ScheduleJobRequest(BaseModel):
    spec: DataGenRequest
    workspace_dir: str = "/Workspace/Shared/lakebase-express"
    quartz_cron: str | None = None      # None = unscheduled (run manually)
    timezone: str = "UTC"


@router.post("/job/submit")
def submit_job(req: OffloadRequest) -> dict:
    """Create/reuse the persistent snapshot job and run it now (re-runnable later
    from the Jobs UI)."""
    try:
        return job_offload.create_job_and_run(req.spec, req.workspace_dir)
    except Exception as exc:
        log.exception("Job offload failed")
        raise HTTPException(status_code=502, detail=f"Job submit failed: {exc}") from exc


@router.post("/job/schedule")
def schedule_job(req: ScheduleJobRequest) -> dict:
    try:
        return job_offload.create_scheduled_job(req.spec, req.workspace_dir, req.quartz_cron, req.timezone)
    except Exception as exc:
        log.exception("Job schedule failed")
        raise HTTPException(status_code=502, detail=f"Schedule failed: {exc}") from exc


# --- Secret scope (feeds the generated snapshot job) -----------------------------


class EnsureSecretsRequest(BaseModel):
    scope: str
    secrets: dict[str, str]      # key -> value; empty values are skipped
    # Optional: connection descriptors so the backend can fill a blank secret
    # value from the persisted credential store (lbx_credentials) instead of
    # requiring the plaintext password client-side after a reload. The source
    # descriptor maps to ``source_key``, the target to ``lakebase_key``.
    source: ConnectionRequest | None = None
    lakebase: LakebaseConnRequest | None = None
    source_key: str | None = None
    lakebase_key: str | None = None


@router.post("/secrets/ensure")
def ensure_secrets(req: EnsureSecretsRequest) -> dict:
    """Create the secret scope if needed and (over)write the given key/value secrets
    so the generated snapshot job can read the source + Lakebase passwords at runtime.

    A secret whose value is blank (the SPA lost it on a reload) is resolved from
    the persisted credential store using the matching connection descriptor, so
    async setup works without re-entering passwords."""
    secrets = dict(req.secrets)
    if req.source and req.source_key and not secrets.get(req.source_key):
        pw, _ = resolve_effective_password(
            req.source.source_type, req.source.host, req.source.database,
            req.source.username, req.source.project_id,
            req.source.password, req.source.secret_ref,
        )
        if pw:
            secrets[req.source_key] = pw
    if req.lakebase and req.lakebase_key and not secrets.get(req.lakebase_key):
        pw, _ = resolve_effective_password(
            LAKEBASE_NAMESPACE, req.lakebase.host, req.lakebase.database,
            req.lakebase.user, req.lakebase.project_id,
            req.lakebase.password, req.lakebase.secret_ref,
        )
        if pw:
            secrets[req.lakebase_key] = pw
    try:
        return secret_setup.ensure_secret_scope(req.scope, secrets)
    except Exception as exc:
        log.exception("Secret scope setup failed")
        raise HTTPException(status_code=502, detail=f"Secret setup failed: {exc}") from exc


# --- Async mode (PySpark snapshot) -----------------------------------------------


class AsyncSetupRequest(BaseModel):
    spec: DataGenRequest
    workspace_dir: str = "/Workspace/Shared/lakebase-express"
    quartz_cron: str | None = None      # None = one-off run; set = recurring schedule
    timezone: str = "UTC"
    # False = create the job but don't start it, so the user can pick/tune the
    # compute in the Jobs UI first. Ignored when quartz_cron is set.
    run_now: bool = True


@router.post("/async/setup")
def setup_async(req: AsyncSetupRequest) -> dict:
    """Provision async mode: submit (one-off), create without running, or schedule
    (recurring) a PySpark snapshot job that copies the source into the plan-created
    Lakebase tables. Returns a UI-friendly summary."""
    try:
        return async_setup.setup_async(
            req.spec, req.workspace_dir, req.quartz_cron, req.timezone, req.run_now
        )
    except Exception as exc:
        log.exception("Async setup failed")
        raise HTTPException(status_code=502, detail=f"Async setup failed: {exc}") from exc


@router.get("/job/status/{run_id}")
def job_status(run_id: int) -> dict:
    try:
        return job_offload.job_status(run_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
