"""Async (job) mode with a Databricks-identity target: the job runs as the signed-in
user and mints its own OAuth credential — no Lakebase password secret."""
import ast
from types import SimpleNamespace

import pytest

from backend.api import migration_routes
from backend.connectors import lakebase_identity
from backend.data_migration.etl_generator import generate
from backend.data_migration.models import DataGenRequest, PostLoadStatement, TableRef
from backend.migration import async_setup, job_offload

_EP = "projects/mig/branches/production/endpoints/primary"


def _spec(**over) -> DataGenRequest:
    base = dict(
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb-host", lakebase_user="ana@corp.com",
        lakebase_auth_mode="databricks", lakebase_endpoint=_EP,
        tables=[TableRef(schema_name="dbo", table_name="Orders")],
        post_load_sql=[PostLoadStatement(name="pk", kind="constraint",
                                         sql='ALTER TABLE "public"."orders" ADD PRIMARY KEY ("Id");')],
    )
    base.update(over)
    return DataGenRequest(**base)


def _python(code: str) -> str:
    return "\n".join(
        line for line in code.splitlines()
        if not line.startswith("# MAGIC") and line != "# Databricks notebook source"
        and line.strip() != "# COMMAND ----------"
    )


# --- Generated notebooks -----------------------------------------------------------


def test_identity_notebooks_mint_oauth_and_read_no_lakebase_secret():
    artifacts = generate(_spec())
    assert len(artifacts) == 2
    for art in artifacts:
        ast.parse(_python(art.code))
        assert f'PG_ENDPOINT = "{_EP}"' in art.code
        assert "generate_database_credential(endpoint=PG_ENDPOINT)" in art.code
        assert "PG_USER = _pg_ws.current_user.me().user_name" in art.code
        assert 'key="lb-key"' not in art.code and "__PG_" not in art.code
        # w.postgres needs a recent SDK even with no run store to report to.
        assert "databricks-sdk>=0.81.0" in art.code
    # Only the source password still comes from the scope.
    assert artifacts[0].code.count("dbutils.secrets.get(") == 1


def test_password_notebooks_are_unchanged_in_behaviour():
    code = generate(_spec(lakebase_auth_mode="password", lakebase_password_secret_key="lb-key"))[0].code
    assert 'dbutils.secrets.get(scope="lakebase-express", key="lb-key")' in code
    assert "generate_database_credential(endpoint=PG_ENDPOINT)" not in code


def test_the_snapshot_renews_the_credential_its_chunks_ship():
    code = generate(_spec())[0].code
    start, end = code.index("_PG_RENEW_S = "), code.index("# Key types a table can be cut")
    minted = iter(["tok-2", "tok-3"])
    ns = {"PG_KWARGS": {"password": "tok-1"}, "_pg_password": lambda: next(minted)}
    exec("import threading, time\n" + code[start:end], ns)
    ns["_renew_pg_credential"]()
    assert ns["PG_KWARGS"]["password"] == "tok-1"  # still fresh
    ns["_PG_RENEW_S"] = -1
    ns["_renew_pg_credential"]()
    assert ns["PG_KWARGS"]["password"] == "tok-2"
    assert code.index("_renew_pg_credential()", code.index("def _copy_chunk")) < code.index(
        "mapInArrow", code.index("def _copy_chunk"))


# --- Job run-as --------------------------------------------------------------------


class _Jobs:
    def __init__(self, may_set_run_as=True):
        self.may_set_run_as = may_set_run_as
        self.updates: list = []
        self.permissions: list = []
        self.ran = False

    def list(self, name=None):
        return iter(())

    def create(self, **_):
        return SimpleNamespace(job_id=100)

    def update(self, job_id=None, new_settings=None):
        if new_settings.run_as is not None and not self.may_set_run_as:
            raise PermissionError("Only workspace admins can set run_as to another user")
        self.updates.append(new_settings)

    def update_permissions(self, job_id=None, access_control_list=None):
        self.permissions.append((job_id, access_control_list))

    def run_now(self, job_id=None, job_parameters=None):
        self.ran = True
        return SimpleNamespace(run_id=555)


@pytest.fixture
def jobs_api(monkeypatch):
    def patch(**kw):
        api = _Jobs(**kw)
        client = SimpleNamespace(
            jobs=api, config=SimpleNamespace(host="https://ws"),
            workspace=SimpleNamespace(mkdirs=lambda p: None, upload=lambda *a, **k: None,
                                      delete=lambda p: None),
        )
        monkeypatch.setattr(job_offload, "workspace_client", lambda: client)
        monkeypatch.setattr(job_offload, "project_name", lambda project_id: "")
        monkeypatch.setattr(async_setup, "with_run_store", lambda req: req)
        seen = {}
        monkeypatch.setattr(async_setup, "check_run_store_access",
                            lambda job_id, as_user="": seen.setdefault("as_user", as_user) and None)
        monkeypatch.setattr(async_setup.async_runs, "record", lambda *a, **k: SimpleNamespace(run_id="r"))
        return api, seen
    return patch


def test_the_job_is_set_to_run_as_the_user_then_runs(jobs_api):
    api, _ = jobs_api()
    out = async_setup.setup_async(_spec(), "/Workspace/Shared/x")
    assert api.updates[0].run_as.user_name == "ana@corp.com"
    assert api.ran and out["run_id"] == 555 and not out.get("run_as_pending")


def test_the_ui_path_creates_the_job_as_the_user_and_never_starts_it(jobs_api):
    """The UI always passes run_now=False: the user runs the job later."""
    api, _ = jobs_api()
    out = async_setup.setup_async(_spec(), "/Workspace/Shared/x", run_now=False)
    assert api.updates[0].run_as.user_name == "ana@corp.com"
    assert not api.ran and out["run_id"] is None and not out["run_as_pending"]
    assert "Run now" in out["note"]


def test_without_rights_to_set_run_as_the_user_is_told_and_nothing_runs(jobs_api):
    api, seen = jobs_api(may_set_run_as=False)
    out = async_setup.setup_async(_spec(), "/Workspace/Shared/x")
    assert not api.ran and out["run_id"] is None
    assert out["run_as_pending"] == "ana@corp.com"
    assert "'Run as'" in out["note"]
    # They can now change it themselves, and the run store expects them.
    acl = api.permissions[0][1][0]
    assert acl.user_name == "ana@corp.com" and acl.permission_level.value == "CAN_MANAGE"
    assert seen["as_user"] == "ana@corp.com"


def test_a_schedule_waits_paused_until_the_user_switches_run_as(jobs_api):
    api, _ = jobs_api(may_set_run_as=False)
    out = async_setup.setup_async(_spec(), "/Workspace/Shared/x", quartz_cron="0 0 * * * ?")
    assert out["run_as_pending"] and "unpause" in out["note"]
    assert api.updates[-1].schedule.pause_status.value == "PAUSED"


def test_password_jobs_keep_their_run_as(jobs_api):
    api, _ = jobs_api()
    async_setup.setup_async(_spec(lakebase_auth_mode="password"), "/Workspace/Shared/x")
    assert api.updates == [] and api.ran


# --- Route ---------------------------------------------------------------------------


def test_the_route_stamps_the_signed_in_user_and_endpoint(monkeypatch):
    monkeypatch.setattr(lakebase_identity, "current_identity", lambda: "Ana@Corp.com")
    monkeypatch.setattr(lakebase_identity, "resolve_endpoint", lambda user, host: _EP)
    out = migration_routes._with_job_identity(_spec(lakebase_user="someone-else", lakebase_endpoint=""))
    assert out.lakebase_user == "Ana@Corp.com" and out.lakebase_endpoint == _EP


def test_the_route_explains_a_missing_identity(monkeypatch):
    def fail():
        raise lakebase_identity.IdentityAuthError("no sign-in")

    monkeypatch.setattr(lakebase_identity, "current_identity", fail)
    with pytest.raises(migration_routes.HTTPException) as exc:
        migration_routes._with_job_identity(_spec())
    assert exc.value.status_code == 400 and "no sign-in" in exc.value.detail


@pytest.mark.parametrize("path", ["/api/migration/job/submit", "/api/migration/job/schedule",
                                  "/api/migration/async/setup"])
def test_no_job_route_lets_a_client_pick_whom_the_job_runs_as(monkeypatch, path):
    """A job runs as lakebase_user in identity mode, so every route must replace it
    with the signed-in user — else a client could run SQL as someone else."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(lakebase_identity, "current_identity", lambda: "ana@corp.com")
    monkeypatch.setattr(lakebase_identity, "resolve_endpoint", lambda user, host: _EP)
    seen = []
    capture = lambda spec, *a, **k: seen.append(spec.lakebase_user) or {"job_id": 1}  # noqa: E731
    monkeypatch.setattr(job_offload, "create_job_and_run", capture)
    monkeypatch.setattr(job_offload, "create_scheduled_job", capture)
    monkeypatch.setattr(async_setup, "setup_async", capture)
    app = FastAPI()
    app.include_router(migration_routes.router)
    spec = _spec(lakebase_user="victim@corp.com").model_dump(mode="json")
    TestClient(app).post(path, json={"spec": spec})
    assert seen == ["ana@corp.com"]
