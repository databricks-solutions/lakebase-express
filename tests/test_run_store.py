"""Persistent run state (backend/run_store.py, backend/run_registry.py).

Run state used to live in a per-module dict, so it died with the process and was
invisible to other workers. The registry keeps memory as the hot path and writes
through to a store — throttled, because progress fires per COPY batch, and
fail-soft, because persistence must never break a migration.
"""
import json
import uuid

import pytest

from backend.api import runs_routes
from backend.migration import async_runs, plan_runs
from backend.migration.models import (
    AsyncRunState,
    BuildPlanRequest,
    RunState,
    TableProgress,
)
from backend.run_registry import RunRegistry
from backend.run_store import (
    MemoryRunStore,
    PostgresRunStore,
    RunRecord,
    RunStore,
    get_run_store,
    run_state_id,
)


class _CountingStore(RunStore):
    """Memory store that records every save, for throttling assertions."""

    def __init__(self) -> None:
        self.saves: list[tuple[str, str, str]] = []
        self.projects: dict[tuple[str, str], str | None] = {}
        self._rows: dict[tuple[str, str], dict] = {}

    def save(self, kind, run_id, status, data, project_id=None):
        self.saves.append((kind, run_id, status))
        self.projects[(kind, run_id)] = project_id
        self._rows[(kind, run_id)] = data

    def load(self, kind, run_id):
        return self._rows.get((kind, run_id))

    def list(self, kind=None, limit=50, project_id=None):
        return [
            RunRecord(kind=k, run_id=r, status="", updated_at="",
                      project_id=self.projects.get((k, r)))
            for (k, r) in self._rows
            if (kind is None or k == kind)
            and (project_id is None or self.projects.get((k, r)) == project_id)
        ]


class _BrokenStore(RunStore):
    """Every operation fails — the app must carry on regardless."""

    def save(self, kind, run_id, status, data, project_id=None):
        raise RuntimeError("lakebase is down")

    def load(self, kind, run_id):
        raise RuntimeError("lakebase is down")

    def list(self, kind=None, limit=50, project_id=None):
        raise RuntimeError("lakebase is down")


RUN_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID_2 = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_ID_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RUN_ID_3 = "33333333-3333-4333-8333-333333333333"


def _state(run_id: str = RUN_ID, status: str = "running") -> RunState:
    return RunState(
        run_id=run_id, status=status,
        tables=[TableProgress(name="dbo.t", target="public.t", total_rows=100)],
    )


def _registry(store: RunStore) -> RunRegistry[RunState]:
    return RunRegistry("sync_run", RunState, store=store)


def _async_registry(monkeypatch, store: RunStore) -> None:
    monkeypatch.setattr(async_runs, "_JOBS", RunRegistry("async_job", AsyncRunState, store=store))
    monkeypatch.setattr(async_runs, "_RUNS", RunRegistry("async_run", AsyncRunState, store=store))


# --- MemoryRunStore ----------------------------------------------------------


def test_memory_store_round_trip():
    store = MemoryRunStore()
    store.save("sync_run", RUN_ID, "running", {"run_id": RUN_ID})
    assert store.load("sync_run", RUN_ID) == {"run_id": RUN_ID}
    assert store.load("sync_run", "missing") is None
    assert store.load("validation", RUN_ID) is None  # kind is part of the key


def test_memory_store_lists_newest_first_and_filters_by_kind():
    store = MemoryRunStore()
    store.save("sync_run", RUN_ID, "success", {})
    store.save("validation", RUN_ID_2, "failed", {})
    store.save("sync_run", RUN_ID_3, "running", {})

    assert [r.run_id for r in store.list()] == [RUN_ID_3, RUN_ID_2, RUN_ID]
    assert [r.run_id for r in store.list(kind="sync_run")] == [RUN_ID_3, RUN_ID]
    assert [r.run_id for r in store.list(limit=1)] == [RUN_ID_3]


# --- PostgresRunStore (fake psycopg connection; no live DB) ------------------


class _FakeCursor:
    """psycopg-cursor stand-in over a shared dict, enough for the store's SQL."""

    def __init__(self, rows, sql, dict_row=False):
        self._rows = rows
        self._sql = sql
        self._dict_row = dict_row
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._sql.append(s)
        if s.startswith(("CREATE TABLE", "CREATE INDEX", "GRANT")):
            self._result = []
        elif s.startswith("INSERT INTO"):
            rid, kind, pid, status, payload = params
            existing = self._rows.get(rid)
            if existing:
                pid = existing[1]  # project_id is immutable on upsert
            self._rows[rid] = (kind, pid, status, json.loads(payload))
            self._result = []
        elif s.startswith("SELECT data FROM"):
            rid, kind = params
            row = self._rows.get(rid)
            self._result = [(row[3],)] if row is not None and row[0] == kind else []
        elif s.startswith("SELECT run_id, kind, project_id, status, updated_at FROM"):
            i, kind, pid = 0, None, None
            if "kind = %s" in s:
                kind, i = params[i], i + 1
            if "project_id = %s::uuid" in s:
                pid, i = params[i], i + 1
            rows = [
                {"run_id": rid, "kind": v[0], "project_id": v[1], "status": v[2],
                 "updated_at": "2026-09-09"}
                for rid, v in self._rows.items()
                if (kind is None or v[0] == kind) and (pid is None or v[1] == pid)
            ]
            self._result = rows[:params[-1]]
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, rows, sql):
        self._rows = rows
        self._sql = sql

    def cursor(self, row_factory=None):
        return _FakeCursor(self._rows, self._sql, dict_row=row_factory is not None)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def pg_store(monkeypatch):
    """Store over a fake psycopg connection, so the real _connect (and its DDL) runs."""
    import psycopg

    rows: dict = {}
    sql: list[str] = []
    monkeypatch.setattr(psycopg, "connect", lambda **kw: _FakeConn(rows, sql))
    store = PostgresRunStore(host="h", database="databricks_postgres", user="u",
                             port=5432, password="p")
    return store, rows, sql


def test_postgres_store_round_trip(pg_store):
    store, _rows, _sql = pg_store
    store.save("sync_run", RUN_ID, "running", {"run_id": RUN_ID, "status": "running"})
    assert store.load("sync_run", RUN_ID) == {"run_id": RUN_ID, "status": "running"}
    assert store.load("sync_run", "nope") is None


def test_postgres_store_upserts_on_save(pg_store):
    store, rows, _sql = pg_store
    store.save("sync_run", RUN_ID, "running", {"n": 1})
    store.save("sync_run", RUN_ID, "success", {"n": 2})
    assert store.load("sync_run", RUN_ID) == {"n": 2}
    assert rows[RUN_ID][2] == "success"
    assert len(rows) == 1  # an upsert, not a second row


def test_postgres_store_list_filters_by_kind_and_limit(pg_store):
    store, _rows, _sql = pg_store
    store.save("sync_run", RUN_ID, "success", {})
    store.save("validation", RUN_ID_2, "failed", {})
    assert {r.run_id for r in store.list()} == {RUN_ID, RUN_ID_2}
    assert [r.kind for r in store.list(kind="validation")] == ["validation"]
    assert len(store.list(limit=1)) == 1


def test_postgres_store_creates_its_table_and_history_index_once(pg_store):
    store, _rows, sql = pg_store
    store.save("sync_run", RUN_ID, "running", {})
    store.save("sync_run", RUN_ID_2, "running", {})
    ddl = " | ".join(sql)
    # run_id is a native uuid primary key, matching lbx_projects.id.
    assert "run_id UUID PRIMARY KEY" in ddl and "project_id UUID" in ddl
    # The table grows one row per run: history by kind, and per project.
    assert "kind, updated_at DESC" in ddl and "_project_idx" in ddl
    # Guarded by _ensured, so the DDL is not re-issued on every connection.
    assert len([s for s in sql if s.startswith("CREATE TABLE")]) == 1


def test_postgres_store_links_a_run_to_its_project(pg_store):
    store, _rows, _sql = pg_store
    store.save("sync_run", RUN_ID, "running", {}, project_id=PROJECT_ID)
    assert [r.project_id for r in store.list()] == [PROJECT_ID]


def test_project_link_is_set_once_and_not_rewritten(pg_store):
    # The upsert deliberately leaves project_id alone: a run cannot change owner.
    store, _rows, _sql = pg_store
    store.save("sync_run", RUN_ID, "running", {}, project_id=PROJECT_ID)
    store.save("sync_run", RUN_ID, "success", {}, project_id=PROJECT_ID_2)
    assert [r.project_id for r in store.list()] == [PROJECT_ID]


def test_postgres_store_lists_the_runs_of_one_project(pg_store):
    store, _rows, _sql = pg_store
    store.save("sync_run", RUN_ID, "success", {}, project_id=PROJECT_ID)
    store.save("validation", RUN_ID_2, "success", {}, project_id=PROJECT_ID_2)
    assert [r.run_id for r in store.list(project_id=PROJECT_ID)] == [RUN_ID]
    assert [r.run_id for r in store.list(project_id=PROJECT_ID_2)] == [RUN_ID_2]


def test_a_run_without_a_project_stores_null_not_an_empty_string(pg_store):
    store, _rows, _sql = pg_store
    store.save("plan_build", RUN_ID, "running", {}, project_id="")
    assert store.list()[0].project_id is None


def test_postgres_store_refuses_a_non_uuid_run_id(pg_store):
    # The old ids were uuid4().hex[:12] — a uuid column would silently reject them,
    # so the store fails loudly instead of losing the row.
    store, _rows, _sql = pg_store
    with pytest.raises(ValueError, match="not a valid UUID"):
        store.save("sync_run", uuid.uuid4().hex[:12], "running", {})


def test_loading_a_non_uuid_run_id_reads_as_missing(pg_store):
    store, _rows, _sql = pg_store
    assert store.load("sync_run", "not-a-uuid") is None


def test_generated_run_ids_are_uuids():
    # plan_runs is the cheapest generator to drive end to end; the other four use
    # the same str(uuid.uuid4()) line.
    run_id = plan_runs.start_run(BuildPlanRequest(translate=False))
    assert uuid.UUID(run_id)


# --- RunRegistry -------------------------------------------------------------


def test_create_persists_immediately_and_get_reads_back():
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    assert store.saves == [("sync_run", RUN_ID, "running")]
    assert reg.get(RUN_ID).run_id == RUN_ID
    assert reg.get("missing") is None


def test_registry_passes_the_project_link_to_the_store():
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state(), PROJECT_ID)
    reg.update(RUN_ID, lambda s: setattr(s, "status", "success"))
    # Carried on every write, not just the first.
    assert store.projects[("sync_run", RUN_ID)] == PROJECT_ID
    assert [r.run_id for r in reg.list(project_id=PROJECT_ID)] == [RUN_ID]


def test_get_returns_a_copy():
    reg = _registry(_CountingStore())
    reg.create(_state())
    got = reg.get(RUN_ID)
    got.tables[0].rows_copied = 999
    assert reg.get(RUN_ID).tables[0].rows_copied == 0


def test_progress_updates_are_throttled(monkeypatch):
    # Progress fires per COPY batch — thousands of writes on a large table.
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 1000.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    for n in (10, 20, 30, 40):
        reg.update(RUN_ID, lambda s, n=n: setattr(s.tables[0], "rows_copied", n))

    assert len(store.saves) == 1  # just the create
    # Memory still has the latest, so polling is unaffected by the throttle.
    assert reg.get(RUN_ID).tables[0].rows_copied == 40


def test_status_change_flushes_even_inside_the_throttle_window(monkeypatch):
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 1000.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    reg.update(RUN_ID, lambda s: setattr(s.tables[0], "rows_copied", 10))
    reg.update(RUN_ID, lambda s: setattr(s, "status", "partial"))

    assert [s[2] for s in store.saves] == ["running", "partial"]


@pytest.mark.parametrize("status", ["success", "failed", "partial"])
def test_terminal_status_always_flushes(monkeypatch, status):
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 1000.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    reg.update(RUN_ID, lambda s: setattr(s, "status", status))
    assert store.saves[-1][2] == status


def test_elapsed_throttle_window_flushes(monkeypatch):
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 0.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    reg.update(RUN_ID, lambda s: setattr(s.tables[0], "rows_copied", 10))
    reg.update(RUN_ID, lambda s: setattr(s.tables[0], "rows_copied", 20))
    assert len(store.saves) == 3


def test_update_of_an_unknown_run_is_a_no_op():
    reg = _registry(_CountingStore())
    reg.update("never-started", lambda s: setattr(s, "status", "success"))  # must not raise


def test_a_late_stale_snapshot_cannot_overwrite_a_finished_run(monkeypatch):
    """Snapshots are taken under the lock but written outside it, so a slow progress
    write can arrive after the terminal one. History must not revert to "running"."""
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 0.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())

    # Take a progress snapshot, then finish the run before that write lands.
    captured: list = []
    monkeypatch.setattr(reg, "_save", captured.append)
    reg.update(RUN_ID, lambda s: setattr(s.tables[0], "rows_copied", 10))
    monkeypatch.undo()
    reg.update(RUN_ID, lambda s: setattr(s, "status", "success"))

    # Replaying the stale snapshot is dropped by the version guard.
    reg._persist(captured[0], 2)
    assert store.load("sync_run", RUN_ID)["status"] == "success"


def test_get_falls_back_to_the_store_for_another_workers_run():
    # Two registries over one store: the second never saw the run in memory.
    store = _CountingStore()
    owner = _registry(store)
    owner.create(_state(status="running"))
    owner.update(RUN_ID, lambda s: setattr(s, "status", "success"))

    other_worker = _registry(store)
    recovered = other_worker.get(RUN_ID)
    assert recovered is not None and recovered.status == "success"


def test_a_broken_store_never_breaks_a_run():
    reg = _registry(_BrokenStore())
    reg.create(_state())                                     # save raises
    reg.update(RUN_ID, lambda s: setattr(s, "status", "success"))
    # Memory is still authoritative for the live run.
    assert reg.get(RUN_ID).status == "success"
    assert reg.list() == []


def test_unreadable_stored_payload_reads_as_missing():
    store = _CountingStore()
    store.save("sync_run", "junk", "running", {"not": "a run state"})
    assert _registry(store).get("junk") is None


def test_list_is_scoped_to_the_registrys_kind():
    store = _CountingStore()
    _registry(store).create(_state(RUN_ID))
    RunRegistry("validation", RunState, store=store).create(_state(RUN_ID_2))
    assert [r.run_id for r in _registry(store).list()] == [RUN_ID]


# --- run kinds + async runs ---------------------------------------------------


def test_the_documented_run_kinds_are_the_ones_registered():
    """Guards the vocabulary stored in lbx_runs.kind against silent drift."""
    from backend.query_parity import runner
    from backend.validation import agent
    from backend.validation import runs as validation_runs
    from backend.migration import runs as migration_runs

    modules = (migration_runs, plan_runs, validation_runs, agent, runner)
    kinds = {m._REGISTRY._kind for m in modules}
    kinds |= {async_runs._JOBS._kind, async_runs._RUNS._kind}
    assert kinds == {
        "sync_run", "async_job", "async_run", "plan_build",
        "validation", "validation_repair", "query_parity",
    }


def _job_result(**over):
    result = {"job_id": 100, "run_id": 555, "url": "https://w/jobs/100",
              "run_url": "https://w/jobs/runs/555", "notebook_path": "/W/x",
              "scheduled": False}
    result.update(over)
    return result


def test_async_run_records_a_submitted_job(monkeypatch):
    store = _CountingStore()
    _async_registry(monkeypatch, store)
    state = async_runs.record(_job_result(), tables_total=3, project_id=PROJECT_ID)

    assert state.status == "submitted"
    assert state.job_id == 100 and state.job_run_id == 555
    assert state.run_url.endswith("/555") and state.tables_total == 3
    # Not the Databricks run id, but derived from it, so the notebooks' first write
    # updates this row instead of adding a second one.
    assert state.run_id == run_state_id(100, 555) and state.run_id != "555"
    assert uuid.UUID(state.run_id)
    assert store.projects[("async_run", state.run_id)] == PROJECT_ID


def test_a_job_created_to_run_later_is_recorded_as_a_job_not_a_run(monkeypatch):
    """'Create job, run later': the job exists but has never executed, so it is not
    a run. Each later execution records itself from inside the notebook."""
    store = _CountingStore()
    _async_registry(monkeypatch, store)
    state = async_runs.record(_job_result(run_id=None, run_url=None), project_id=PROJECT_ID)

    assert state.status == "created" and state.job_run_id is None
    assert store.projects[("async_job", state.run_id)] == PROJECT_ID
    assert [r.kind for r in store.list()] == ["async_job"]
    # No run id to derive one from, so this row can never collide with a run's.
    assert state.run_id != run_state_id(100, None)


def test_scheduled_async_run_is_recorded_as_scheduled(monkeypatch):
    store = _CountingStore()
    _async_registry(monkeypatch, store)
    state = async_runs.record(_job_result(run_id=None), quartz_cron="0 0 * * * ?")
    assert state.status == "scheduled" and state.scheduled is True
    assert state.quartz_cron == "0 0 * * * ?"
    assert [r.kind for r in store.list()] == ["async_job"]


def test_each_execution_of_one_job_gets_its_own_run_id():
    """The id the notebooks derive is per job run, so re-running the same job from
    the Jobs UI adds a row instead of overwriting the previous one."""
    first, second = run_state_id(100, 555), run_state_id(100, 556)
    assert first != second
    # Stable, so every task of one chain updates the same row.
    assert first == run_state_id(100, 555)
    # And distinct across jobs that happen to share a run id.
    assert first != run_state_id(101, 555)


def test_async_and_sync_runs_share_the_project_but_not_the_kind(monkeypatch):
    store = _CountingStore()
    _async_registry(monkeypatch, store)
    _registry(store).create(_state(), PROJECT_ID)
    async_state = async_runs.record(_job_result(), project_id=PROJECT_ID)

    both = store.list(project_id=PROJECT_ID)
    assert {r.kind for r in both} == {"sync_run", "async_run"}
    assert {r.run_id for r in both} == {RUN_ID, async_state.run_id}


# --- backend selection + history route ---------------------------------------


def test_store_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("LBX_RUNS_BACKEND", raising=False)
    monkeypatch.delenv("LBX_PROJECTS_BACKEND", raising=False)
    get_run_store.cache_clear()
    assert isinstance(get_run_store(), MemoryRunStore)
    get_run_store.cache_clear()


def _pg_env(monkeypatch):
    monkeypatch.delenv("LBX_RUNS_BACKEND", raising=False)
    monkeypatch.setenv("LBX_PROJECTS_BACKEND", "postgres")
    monkeypatch.setenv("LBX_PROJECTS_PG_HOST", "ep-x.database.eastus2.azuredatabricks.net")
    monkeypatch.setenv("LBX_PROJECTS_PG_USER", "u")
    monkeypatch.setenv("LBX_PROJECTS_PG_PASSWORD", "p")
    monkeypatch.delenv("LBX_PROJECTS_PG_ENDPOINT", raising=False)


ENDPOINT = "projects/p/branches/production/endpoints/primary"


def test_the_endpoint_is_resolved_from_the_host_not_configured(monkeypatch):
    """The endpoint path cannot be derived from the hostname, so it is looked up —
    otherwise every deployment has to hand-configure it before a job can report."""
    from backend import config

    _pg_env(monkeypatch)
    seen = []
    monkeypatch.setattr(config, "lakebase_endpoint", lambda host: seen.append(host) or ENDPOINT)
    get_run_store.cache_clear()
    store = get_run_store()

    assert seen == ["ep-x.database.eastus2.azuredatabricks.net"]
    assert store.notebook_config()["endpoint"] == ENDPOINT
    get_run_store.cache_clear()


def test_a_configured_endpoint_overrides_the_lookup(monkeypatch):
    """The escape hatch for an identity that cannot list Lakebase projects."""
    from backend import config

    _pg_env(monkeypatch)
    monkeypatch.setenv("LBX_PROJECTS_PG_ENDPOINT", "  projects/o/branches/b/endpoints/e  ")
    monkeypatch.setattr(config, "lakebase_endpoint", lambda host: pytest.fail("looked up anyway"))
    get_run_store.cache_clear()

    assert get_run_store().notebook_config()["endpoint"] == "projects/o/branches/b/endpoints/e"
    get_run_store.cache_clear()


def test_an_unresolvable_endpoint_leaves_reporting_off_not_the_app_broken(monkeypatch):
    """No endpoint means jobs cannot report, which provisioning warns about — it must
    not stop the app keeping run state itself."""
    from backend import config

    _pg_env(monkeypatch)
    monkeypatch.setattr(config, "lakebase_endpoint", lambda host: "")
    get_run_store.cache_clear()
    store = get_run_store()

    assert isinstance(store, PostgresRunStore)
    assert store.notebook_config() is None
    get_run_store.cache_clear()


def test_store_follows_the_project_backend(monkeypatch):
    # Postgres-backed projects imply a Lakebase to keep runs in too.
    monkeypatch.delenv("LBX_RUNS_BACKEND", raising=False)
    monkeypatch.setenv("LBX_PROJECTS_BACKEND", "postgres")
    monkeypatch.setenv("LBX_PROJECTS_PG_HOST", "h")
    monkeypatch.setenv("LBX_PROJECTS_PG_USER", "u")
    monkeypatch.setenv("LBX_PROJECTS_PG_PASSWORD", "p")
    get_run_store.cache_clear()
    assert isinstance(get_run_store(), PostgresRunStore)
    get_run_store.cache_clear()


def test_unavailable_postgres_store_falls_back_to_memory(monkeypatch):
    monkeypatch.setenv("LBX_RUNS_BACKEND", "postgres")
    monkeypatch.delenv("LBX_PROJECTS_PG_HOST", raising=False)  # required, so construction fails
    get_run_store.cache_clear()
    assert isinstance(get_run_store(), MemoryRunStore)
    get_run_store.cache_clear()


def test_history_route_reports_whether_history_survives_a_restart(monkeypatch):
    store = MemoryRunStore()
    store.save("sync_run", RUN_ID, "success", {})
    monkeypatch.setattr(runs_routes, "get_run_store", lambda: store)

    result = runs_routes.history(kind=None, limit=50)
    assert result.persistent is False  # memory store — history dies with the process
    assert [r.run_id for r in result.runs] == [RUN_ID]


# --- OAuth run-state reporting from the job -----------------------------------


def test_notebook_config_needs_an_endpoint_path():
    # OAuth is the only auth for this path, and minting needs the endpoint
    # resource path — a hostname is not enough.
    without = PostgresRunStore(host="h", database="d", user="u", port=5432, password="p")
    assert without.notebook_config() is None

    with_ep = PostgresRunStore(host="h", database="d", user="u", port=5432, password="p",
                               endpoint="projects/p/branches/production/endpoints/primary")
    assert with_ep.notebook_config() == {
        "host": "h", "port": 5432, "database": "d", "table": "lbx_runs",
        "endpoint": "projects/p/branches/production/endpoints/primary",
    }


def test_memory_store_offers_no_notebook_config():
    # Nothing for a job to write to, so notebooks are generated without reporting.
    assert MemoryRunStore().notebook_config() is None


def test_grant_writer_is_least_privilege_and_keeps_ownership(pg_store):
    store, _rows, sql = pg_store
    store.grant_writer("sp-client-id")

    granted = [s for s in sql if s.startswith("GRANT")]
    assert granted == [
        'GRANT USAGE ON SCHEMA public TO "sp-client-id"',
        'GRANT SELECT, INSERT, UPDATE ON "lbx_runs" TO "sp-client-id"',
    ]
    # No DELETE and no DDL: the job records runs, the app stays owner.
    assert not any("DELETE" in s or "OWNER" in s for s in granted)


def test_grant_writer_quotes_an_identity_safely(pg_store):
    # Databricks identities are emails or client ids, so the role name must be
    # quoted; an embedded quote must not break out of the identifier.
    store, _rows, sql = pg_store
    store.grant_writer('od"d@example.com')
    assert 'TO "od""d@example.com"' in " | ".join(s for s in sql if s.startswith("GRANT"))
