"""Resuming an async (Databricks job) snapshot run.

Sync mode has been resumable since the run store kept per-table state; async mode
re-copied every table, because the generated notebooks recorded per-task progress
only. The loader now checkpoints each table as it settles — into the same run-state
row — and takes a ``resume_from`` job parameter that skips what that run loaded.
Each table is truncated and copied in one pass, so "already loaded" is exact.
"""
import ast
import json
import re
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import migration_routes
from backend.data_migration.etl_generator import generate
from backend.data_migration.models import (
    DataGenRequest,
    PostLoadStatement,
    RunStoreTarget,
    TableRef,
)
from backend.migration import async_runs, async_setup, job_offload
from backend.migration.models import AsyncRunState, AsyncTableProgress
from backend.run_registry import RunRegistry
from backend.run_store import MemoryRunStore, run_state_id


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """A private store per test, so listing a project's runs sees only its own."""
    monkeypatch.setattr(
        async_runs, "_RUNS", RunRegistry("async_run", AsyncRunState, store=MemoryRunStore())
    )


@pytest.fixture
def project() -> str:
    return str(uuid.uuid4())


def _spec(**over) -> DataGenRequest:
    base = dict(
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb-host", lakebase_user="lbuser",
        lakebase_password_secret_key="lb-key",
        tables=[
            TableRef(schema_name="dbo", table_name="Orders", primary_key=["OrderId"]),
            TableRef(schema_name="Sales", table_name="Invoice"),
        ],
        run_store=RunStoreTarget(
            host="rs-host", database="databricks_postgres", table="lbx_runs",
            endpoint="projects/p/branches/b/endpoints/primary",
        ),
    )
    base.update(over)
    return DataGenRequest(**base)


def _state(project_id: str, **over) -> AsyncRunState:
    base = dict(run_id=str(uuid.uuid4()), status="failed", job_id=7, job_run_id=99,
                tables_total=3)
    base.update(over)
    state = AsyncRunState(**base)
    async_runs._RUNS.create(state, project_id)
    return state


def _parse_cells(code: str) -> None:
    """The generated notebook is valid Python once the notebook magics are stripped."""
    ast.parse("\n".join(
        line for line in code.splitlines()
        if not line.startswith("# MAGIC")
        and line != "# Databricks notebook source"
        and line.strip() != "# COMMAND ----------"
    ))


def _loaded(*names) -> dict[str, AsyncTableProgress]:
    return {n: AsyncTableProgress(status="success", rows_copied=10) for n in names}


# --- The generated loader ----------------------------------------------------------


def test_the_loader_takes_a_run_to_resume_as_a_job_parameter():
    """A scheduled or manual run must still be a full snapshot, so resuming is opt-in
    per run — a job parameter, defaulting to empty."""
    code = generate(_spec())[0].code
    assert 'dbutils.widgets.text("resume_from", "")' in code
    assert "def _resume_checkpoint()" in code


def test_the_loader_copies_only_what_the_resumed_run_left():
    code = generate(_spec())[0].code
    _parse_cells(code)
    assert "loaded, inherited_fks = _resume_checkpoint()" in code
    # The remainder drives both the copy and the FK drop — a skipped table is never
    # truncated, and its FKs are already down.
    assert 'todo = [spec for spec in TABLES if f"{spec[0]}.{spec[1]}" not in loaded]' in code
    assert "drop_target_fks(todo)" in code
    assert "pool.submit(snapshot_table, *spec): spec for spec in todo" in code


def test_each_table_is_checkpointed_as_it_settles():
    """The checkpoint is the resume: written per table, not once at the end, or a
    run that dies mid-copy leaves nothing to resume from."""
    code = generate(_spec())[0].code
    assert code.count("_checkpoint(tables=progress)") == 2   # the skipped seed, then per table
    assert '"status": "success", "rows_copied": rows' in code
    assert '"status": "failed", "error": str(exc)' in code


def test_the_dropped_foreign_keys_are_persisted_and_inherited():
    """They were held in a local variable: a run that died mid-load took the only
    copy of their definitions with it, leaving the target with no FKs at all."""
    code = generate(_spec())[0].code
    assert "_checkpoint(dropped_fks=dropped_fks)" in code
    assert "_merge_fks(inherited_fks, drop_target_fks(todo))" in code
    # Restored (or reported) — a resume of this run must not try again.
    assert "_checkpoint(dropped_fks=[])" in code


def test_without_a_run_store_the_resume_calls_still_work():
    """No store, no checkpoints, so nothing to resume from — but the notebook must
    stay valid Python, since the calls are in the template either way."""
    code = generate(_spec(run_store=None))[0].code
    assert "def _checkpoint(**keys):\n    pass" in code
    stub = code[code.index("def _resume_checkpoint():"):]
    assert "return {}, []" in stub[:stub.index("# COMMAND")]
    _parse_cells(code)


# --- What the loader writes and reads ---------------------------------------------


class _Cur:
    def __init__(self, sink, row=None):
        self._sink, self._row = sink, row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append((sql, params))

    def fetchone(self):
        return self._row


class _Conn:
    closed = False

    def __init__(self, sink, row=None):
        self._sink, self._row = sink, row

    def cursor(self):
        return _Cur(self._sink, self._row)

    def commit(self):
        pass


def _run_state_cell(monkeypatch, code, row=None, resume_from="prior-run"):
    """Exec the loader's run-state cell; (namespace, writes)."""
    start = code.index("RUN_STORE_HOST = ")
    block = code[start:code.index("# COMMAND", start)]
    writes: list[tuple] = []
    monkeypatch.setitem(sys.modules, "psycopg",
                        SimpleNamespace(connect=lambda **kw: _Conn(writes, row)))
    monkeypatch.setitem(sys.modules, "databricks.sdk", SimpleNamespace(
        WorkspaceClient=lambda: SimpleNamespace(
            postgres=SimpleNamespace(
                generate_database_credential=lambda endpoint: SimpleNamespace(token="tok")),
            current_user=SimpleNamespace(me=lambda: SimpleNamespace(user_name="me@x.com")))))
    widgets = SimpleNamespace(
        text=lambda *a, **k: None,
        get=lambda k: {"job_id": "100", "job_run_id": "555",
                       "resume_from": resume_from}.get(k, ""),
    )
    ns: dict = {"dbutils": SimpleNamespace(widgets=widgets)}
    exec(block, ns)
    return ns, writes


def test_a_checkpoint_merges_into_the_row_it_shares_with_every_task(monkeypatch):
    """Whole-row writes would erase the job url, task states and table count already
    recorded there."""
    ns, writes = _run_state_cell(monkeypatch, generate(_spec())[0].code)
    ns["_checkpoint"](tables={"dbo.Orders": {"status": "success", "rows_copied": 5}})

    sql, params = writes[-1]
    assert "data = \"lbx_runs\".data || EXCLUDED.data" in sql
    assert "data = EXCLUDED.data," not in sql
    payload = json.loads(params[4])
    # Written under the id every task of this job run derives, ours included.
    assert payload["run_id"] == run_state_id(100, 555)
    assert payload["tables"]["dbo.Orders"]["rows_copied"] == 5


def test_the_checkpoint_upsert_is_valid_postgres(monkeypatch):
    """There is no local Postgres to run it against, so parse what the notebook
    actually builds."""
    import pglast

    ns, writes = _run_state_cell(monkeypatch, generate(_spec())[0].code)
    ns["_checkpoint"](dropped_fks=[["public.orders", "fk_o", "FOREIGN KEY (c) REFERENCES t(c)"]])
    counter = iter(range(1, 20))
    pglast.parse_sql(re.sub(r"%s", lambda _m: f"${next(counter)}", writes[-1][0]))


def test_a_resume_reads_only_the_tables_that_finished(monkeypatch):
    """A table left running or failed is copied again; one loaded in full is not."""
    row = ({
        "tables": {
            "dbo.Orders": {"status": "success", "rows_copied": 10},
            "dbo.Items": {"status": "skipped", "rows_copied": 4},
            "dbo.Audit": {"status": "failed", "error": "boom"},
            "dbo.Big": {"status": "running"},
        },
        "dropped_fks": [["public.orders", "fk_o", "FOREIGN KEY (c) REFERENCES t(c)"]],
    },)
    ns, writes = _run_state_cell(monkeypatch, generate(_spec())[0].code, row=row)
    loaded, fks = ns["_resume_checkpoint"]()

    assert sorted(loaded) == ["dbo.Items", "dbo.Orders"]
    assert fks == [("public.orders", "fk_o", "FOREIGN KEY (c) REFERENCES t(c)")]
    # Read from the run named by the parameter, not from this run's own row.
    assert writes[-1][1] == ("prior-run",)


def test_a_repair_carries_on_from_its_own_row(monkeypatch):
    """Databricks reuses the original run's parameters on a repair, so resume_from is
    empty there — and the run it must skip work from is itself. Without this, repairing
    a failed task re-copied every table."""
    row = ({"tables": {"dbo.Orders": {"status": "success", "rows_copied": 10}}},)
    ns, writes = _run_state_cell(
        monkeypatch, generate(_spec())[0].code, row=row, resume_from=""
    )
    loaded, _fks = ns["_resume_checkpoint"]()

    assert sorted(loaded) == ["dbo.Orders"]
    # Read under the id every task of this job run derives, not another run's.
    assert writes[-1][1] == (run_state_id(100, 555),)


def test_a_first_attempt_has_nothing_to_carry_on_from(monkeypatch):
    """The app writes the row when it triggers the run, so the row exists before the
    loader starts — a plain or scheduled snapshot must still copy every table."""
    row = ({"run_id": run_state_id(100, 555), "status": "running", "tables_total": 3},)
    ns, _writes = _run_state_cell(
        monkeypatch, generate(_spec())[0].code, row=row, resume_from=""
    )
    assert ns["_resume_checkpoint"]() == ({}, [])


def test_an_unreadable_checkpoint_copies_everything(monkeypatch):
    """Failing closed would be worse: bookkeeping must never fail a migration, and a
    full copy is correct, just slower."""
    ns, _writes = _run_state_cell(monkeypatch, generate(_spec())[0].code, row=None)
    assert ns["_resume_checkpoint"]() == ({}, [])


def test_the_run_records_what_it_resumed(monkeypatch):
    """So the app never offers a run that a later one already continued — including a
    resume started from the Jobs UI, which the app never saw."""
    ns, writes = _run_state_cell(monkeypatch, generate(_spec())[0].code)
    ns["_report_run_state"]("running")
    assert json.loads(writes[-1][1][4])["resumed_from"] == "prior-run"


# --- Triggering a resume -----------------------------------------------------------


class _FakeJobs:
    def __init__(self):
        self.created_with = None
        self.run_now_params: list[dict | None] = []

    def list(self, name=None):
        return iter(())

    def create(self, name=None, tasks=None, schedule=None, tags=None, parameters=None):
        self.created_with = {"tasks": tasks, "parameters": parameters}
        return SimpleNamespace(job_id=100)

    def reset(self, job_id=None, new_settings=None):
        pass

    def run_now(self, job_id=None, job_parameters=None):
        self.run_now_params.append(job_parameters)
        return SimpleNamespace(run_id=555)

    def get(self, job_id=None):
        return SimpleNamespace(settings=SimpleNamespace(run_as=None), run_as_user_name="me@x.com")


def _patch_workspace(monkeypatch) -> _FakeJobs:
    jobs_api = _FakeJobs()
    client = SimpleNamespace(
        jobs=jobs_api,
        workspace=SimpleNamespace(mkdirs=lambda p: None, delete=lambda p: None,
                                  upload=lambda *a, **k: None),
        config=SimpleNamespace(host="https://ws"),
        current_user=SimpleNamespace(me=lambda: SimpleNamespace(user_name="me@x.com")),
    )
    monkeypatch.setattr(job_offload, "workspace_client", lambda: client)
    monkeypatch.setattr(async_setup, "workspace_client", lambda: client)
    monkeypatch.setattr(job_offload, "project_name", lambda project_id: "")
    monkeypatch.setattr(async_setup, "with_run_store", lambda req: req)
    monkeypatch.setattr(async_setup, "check_run_store_access", lambda job_id: None)
    return jobs_api


def test_the_job_declares_the_resume_parameter_so_a_run_can_set_it(monkeypatch):
    jobs_api = _patch_workspace(monkeypatch)
    async_setup.setup_async(_spec(), "/Workspace/Shared/x")
    declared = jobs_api.created_with["parameters"]
    assert [(p.name, p.default) for p in declared] == [("resume_from", "")]


def test_a_resume_passes_the_run_id_to_the_job_run(monkeypatch):
    jobs_api = _patch_workspace(monkeypatch)
    async_setup.setup_async(_spec(), "/Workspace/Shared/x", resume_from="prior-run")
    assert jobs_api.run_now_params == [{"resume_from": "prior-run"}]


def test_an_ordinary_run_sets_no_parameter_so_it_copies_everything(monkeypatch):
    jobs_api = _patch_workspace(monkeypatch)
    async_setup.setup_async(_spec(), "/Workspace/Shared/x")
    assert jobs_api.run_now_params == [None]


def test_a_resumed_run_is_recorded_as_continuing_the_old_one(monkeypatch, project):
    _patch_workspace(monkeypatch)
    out = async_setup.setup_async(
        _spec(project_id=project), "/Workspace/Shared/x", resume_from="prior-run"
    )
    state = async_runs.get_run(out["lbx_run_id"])
    assert state.resumed_from == "prior-run"
    assert state.run_id == run_state_id(100, 555)   # the id the notebooks will write to


def test_an_unreadable_job_state_counts_as_still_going(monkeypatch):
    """The Jobs API returning nothing useful must not be read as "finished" — that
    would offer a resume of a job that is still copying."""
    monkeypatch.setattr(job_offload, "job_status", lambda run_id: {"life_cycle_state": ""})
    assert job_offload.run_active(1) is None

    def boom(run_id):
        raise RuntimeError("410 gone")

    monkeypatch.setattr(job_offload, "job_status", boom)
    assert job_offload.run_active(1) is None


def test_a_terminated_job_run_reads_as_finished(monkeypatch):
    monkeypatch.setattr(job_offload, "job_status",
                        lambda run_id: {"life_cycle_state": "RunLifeCycleState.TERMINATED"})
    assert job_offload.run_active(1) is False
    monkeypatch.setattr(job_offload, "job_status",
                        lambda run_id: {"life_cycle_state": "RunLifeCycleState.RUNNING"})
    assert job_offload.run_active(1) is True


# --- Which run is offered ----------------------------------------------------------


def test_a_failed_run_with_loaded_tables_is_resumable():
    state = _state("p", tables=_loaded("dbo.Orders"))
    assert async_runs.is_resumable(state)
    assert async_runs.tables_left(state) == 2


def test_a_run_with_nothing_loaded_is_not_offered():
    """Resuming it would be the same full copy — 'run the snapshot' already does that."""
    assert not async_runs.is_resumable(_state("p"))


def test_a_run_that_loaded_everything_is_not_offered():
    assert not async_runs.is_resumable(
        _state("p", tables=_loaded("a", "b", "c"), tables_total=3)
    )


def test_a_failed_run_still_holding_dropped_foreign_keys_is_resumable():
    """Nothing loaded, but the FKs it dropped are only recorded here — the resume is
    what puts them back."""
    state = _state("p", dropped_fks=[("public.orders", "fk_o", "FOREIGN KEY (c) REFERENCES t(c)")])
    assert async_runs.is_resumable(state)


def test_a_live_job_run_is_never_resumable():
    """Two jobs loading one table would fight over the same TRUNCATE."""
    state = _state("p", status="running", tables=_loaded("dbo.Orders"))
    assert not async_runs.is_resumable(state, job_active=True)
    # A task can be mid-COPY for an hour without recording anything, so an unknown
    # state counts as live.
    assert not async_runs.is_resumable(state, job_active=None)
    assert async_runs.is_resumable(state, job_active=False)


def test_a_finished_run_is_not_resumable():
    assert not async_runs.is_resumable(
        _state("p", status="success", tables=_loaded("dbo.Orders"))
    )


def test_tables_left_falls_back_to_the_checkpoints_when_the_total_is_unknown():
    """A run recorded only by its notebook (triggered from the Jobs UI) has no total."""
    state = _state("p", tables_total=0, tables={
        **_loaded("dbo.Orders"),
        "dbo.Items": AsyncTableProgress(status="failed", error="boom"),
    })
    assert async_runs.tables_left(state) == 1


def test_the_newest_resumable_run_is_offered(project):
    _state(project, tables=_loaded("dbo.Orders"))
    newest = _state(project, tables=_loaded("dbo.Orders", "dbo.Items"))
    assert async_runs.find_resumable(project).run_id == newest.run_id


def test_a_run_another_already_resumed_is_never_offered_again(project):
    """Whatever it left behind is that resume's business now."""
    first = _state(project, tables=_loaded("dbo.Orders"))
    _state(project, resumed_from=first.run_id, tables=_loaded("dbo.Orders", "dbo.Items"),
           tables_total=2)
    assert async_runs.find_resumable(project) is None


def test_another_project_is_never_offered(project):
    _state(str(uuid.uuid4()), tables=_loaded("dbo.Orders"))
    assert async_runs.find_resumable(project) is None


# --- API ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(migration_routes.router)
    return TestClient(app)


def test_the_resume_offer_is_served_with_what_it_would_skip(client, project, monkeypatch):
    monkeypatch.setattr(job_offload, "run_active", lambda job_run_id: False)
    state = _state(project, tables=_loaded("dbo.Orders", "dbo.Items"), run_url="https://ws/run")

    run = client.get(f"/api/migration/async/resumable?project_id={project}").json()["run"]
    assert run["run_id"] == state.run_id
    assert (run["tables_total"], run["tables_left"], run["rows_copied"]) == (3, 1, 20)
    assert run["job_run_id"] == 99 and run["run_url"] == "https://ws/run"


def test_nothing_to_resume_is_not_an_error(client, project):
    assert client.get(f"/api/migration/async/resumable?project_id={project}").json() == {"run": None}


def _setup_body(project: str, **over) -> dict:
    body = {
        "spec": _spec(project_id=project).model_dump(mode="json"),
        "workspace_dir": "/Workspace/Shared/x",
        "quartz_cron": None,
        "timezone": "UTC",
        "run_now": True,
    }
    body.update(over)
    return body


def test_resuming_an_unknown_run_is_refused(client, project):
    r = client.post("/api/migration/async/setup",
                    json=_setup_body(project, resume_from=str(uuid.uuid4())))
    assert r.status_code == 404


def test_resuming_another_project_s_run_is_refused(client, project, monkeypatch):
    """Its checkpoint names tables this project may not even be loading."""
    monkeypatch.setattr(job_offload, "run_active", lambda job_run_id: False)
    other = _state(str(uuid.uuid4()), tables=_loaded("dbo.Orders"))
    r = client.post("/api/migration/async/setup", json=_setup_body(project, resume_from=other.run_id))
    assert r.status_code == 409
    assert "another migration project" in r.json()["detail"]


def test_resuming_a_live_run_is_refused(client, project, monkeypatch):
    monkeypatch.setattr(job_offload, "run_active", lambda job_run_id: True)
    state = _state(project, status="running", tables=_loaded("dbo.Orders"))
    r = client.post("/api/migration/async/setup", json=_setup_body(project, resume_from=state.run_id))
    assert r.status_code == 409
    assert "still going" in r.json()["detail"]


def test_a_resume_cannot_be_scheduled(client, project, monkeypatch):
    """There is nothing to continue in a schedule, or in a job left unstarted."""
    monkeypatch.setattr(job_offload, "run_active", lambda job_run_id: False)
    state = _state(project, tables=_loaded("dbo.Orders"))
    for over in ({"quartz_cron": "0 0 * * * ?"}, {"run_now": False}):
        r = client.post("/api/migration/async/setup",
                        json=_setup_body(project, resume_from=state.run_id, **over))
        assert r.status_code == 400, over


def test_the_resume_reaches_provisioning(client, project, monkeypatch):
    monkeypatch.setattr(job_offload, "run_active", lambda job_run_id: False)
    state = _state(project, tables=_loaded("dbo.Orders"))
    seen: dict = {}

    def fake_setup(*args):
        seen["args"] = args
        return {"ok": True}

    monkeypatch.setattr(migration_routes.async_setup, "setup_async", fake_setup)
    r = client.post("/api/migration/async/setup", json=_setup_body(project, resume_from=state.run_id))
    assert r.status_code == 200
    assert seen["args"][-1] == state.run_id
