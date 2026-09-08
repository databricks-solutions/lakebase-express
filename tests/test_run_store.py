"""Persistent run state (backend/run_store.py, backend/run_registry.py).

Run state used to live in a per-module dict, so it died with the process and was
invisible to other workers. The registry keeps memory as the hot path and writes
through to a store — throttled, because progress fires per COPY batch, and
fail-soft, because persistence must never break a migration.
"""
import json

import pytest

from backend.api import runs_routes
from backend.migration.models import RunState, TableProgress
from backend.run_registry import RunRegistry
from backend.run_store import (
    MemoryRunStore,
    PostgresRunStore,
    RunRecord,
    RunStore,
    get_run_store,
)


class _CountingStore(RunStore):
    """Memory store that records every save, for throttling assertions."""

    def __init__(self) -> None:
        self.saves: list[tuple[str, str, str]] = []
        self._rows: dict[tuple[str, str], dict] = {}

    def save(self, kind, run_id, status, data):
        self.saves.append((kind, run_id, status))
        self._rows[(kind, run_id)] = data

    def load(self, kind, run_id):
        return self._rows.get((kind, run_id))

    def list(self, kind=None, limit=50):
        return [
            RunRecord(kind=k, run_id=r, status="", updated_at="")
            for (k, r) in self._rows
            if kind is None or k == kind
        ]


class _BrokenStore(RunStore):
    """Every operation fails — the app must carry on regardless."""

    def save(self, kind, run_id, status, data):
        raise RuntimeError("lakebase is down")

    def load(self, kind, run_id):
        raise RuntimeError("lakebase is down")

    def list(self, kind=None, limit=50):
        raise RuntimeError("lakebase is down")


def _state(run_id: str = "abc123", status: str = "running") -> RunState:
    return RunState(
        run_id=run_id, status=status,
        tables=[TableProgress(name="dbo.t", target="public.t", total_rows=100)],
    )


def _registry(store: RunStore) -> RunRegistry[RunState]:
    return RunRegistry("data_migration", RunState, store=store)


# --- MemoryRunStore ----------------------------------------------------------


def test_memory_store_round_trip():
    store = MemoryRunStore()
    store.save("data_migration", "r1", "running", {"run_id": "r1"})
    assert store.load("data_migration", "r1") == {"run_id": "r1"}
    assert store.load("data_migration", "missing") is None
    assert store.load("validation", "r1") is None  # kind is part of the key


def test_memory_store_lists_newest_first_and_filters_by_kind():
    store = MemoryRunStore()
    store.save("data_migration", "r1", "success", {})
    store.save("validation", "r2", "failed", {})
    store.save("data_migration", "r3", "running", {})

    assert [r.run_id for r in store.list()] == ["r3", "r2", "r1"]
    assert [r.run_id for r in store.list(kind="data_migration")] == ["r3", "r1"]
    assert [r.run_id for r in store.list(limit=1)] == ["r3"]


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
        if s.startswith("CREATE TABLE") or s.startswith("CREATE INDEX"):
            self._result = []
        elif s.startswith("INSERT INTO"):
            kind, run_id, status, payload = params
            self._rows[(kind, run_id)] = (status, json.loads(payload))
            self._result = []
        elif s.startswith("SELECT data FROM"):
            row = self._rows.get((params[0], params[1]))
            self._result = [(row[1],)] if row is not None else []
        elif s.startswith("SELECT kind, run_id, status, updated_at FROM"):
            kind = params[0] if "WHERE kind" in s else None
            limit = params[-1]
            rows = [
                {"kind": k, "run_id": r, "status": v[0], "updated_at": "2026-09-08"}
                for (k, r), v in self._rows.items()
                if kind is None or k == kind
            ]
            self._result = rows[:limit]
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
    store.save("data_migration", "r1", "running", {"run_id": "r1", "status": "running"})
    assert store.load("data_migration", "r1") == {"run_id": "r1", "status": "running"}
    assert store.load("data_migration", "nope") is None


def test_postgres_store_upserts_on_save(pg_store):
    store, rows, _sql = pg_store
    store.save("data_migration", "r1", "running", {"n": 1})
    store.save("data_migration", "r1", "success", {"n": 2})
    assert store.load("data_migration", "r1") == {"n": 2}
    assert rows[("data_migration", "r1")][0] == "success"
    assert len(rows) == 1  # an upsert, not a second row


def test_postgres_store_list_filters_by_kind_and_limit(pg_store):
    store, _rows, _sql = pg_store
    store.save("data_migration", "r1", "success", {})
    store.save("validation", "r2", "failed", {})
    assert {r.run_id for r in store.list()} == {"r1", "r2"}
    assert [r.kind for r in store.list(kind="validation")] == ["validation"]
    assert len(store.list(limit=1)) == 1


def test_postgres_store_creates_its_table_and_history_index_once(pg_store):
    store, _rows, sql = pg_store
    store.save("data_migration", "r1", "running", {})
    store.save("data_migration", "r2", "running", {})
    ddl = " | ".join(sql)
    assert "CREATE TABLE IF NOT EXISTS" in ddl and "PRIMARY KEY (kind, run_id)" in ddl
    # The table grows one row per run, so history queries need the index.
    assert "CREATE INDEX IF NOT EXISTS" in ddl and "updated_at DESC" in ddl
    # Guarded by _ensured, so the DDL is not re-issued on every connection.
    assert len([s for s in sql if s.startswith("CREATE TABLE")]) == 1


# --- RunRegistry -------------------------------------------------------------


def test_create_persists_immediately_and_get_reads_back():
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    assert store.saves == [("data_migration", "abc123", "running")]
    assert reg.get("abc123").run_id == "abc123"
    assert reg.get("missing") is None


def test_get_returns_a_copy():
    reg = _registry(_CountingStore())
    reg.create(_state())
    got = reg.get("abc123")
    got.tables[0].rows_copied = 999
    assert reg.get("abc123").tables[0].rows_copied == 0


def test_progress_updates_are_throttled(monkeypatch):
    # Progress fires per COPY batch — thousands of writes on a large table.
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 1000.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    for n in (10, 20, 30, 40):
        reg.update("abc123", lambda s, n=n: setattr(s.tables[0], "rows_copied", n))

    assert len(store.saves) == 1  # just the create
    # Memory still has the latest, so polling is unaffected by the throttle.
    assert reg.get("abc123").tables[0].rows_copied == 40


def test_status_change_flushes_even_inside_the_throttle_window(monkeypatch):
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 1000.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    reg.update("abc123", lambda s: setattr(s.tables[0], "rows_copied", 10))
    reg.update("abc123", lambda s: setattr(s, "status", "partial"))

    assert [s[2] for s in store.saves] == ["running", "partial"]


@pytest.mark.parametrize("status", ["success", "failed", "partial"])
def test_terminal_status_always_flushes(monkeypatch, status):
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 1000.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    reg.update("abc123", lambda s: setattr(s, "status", status))
    assert store.saves[-1][2] == status


def test_elapsed_throttle_window_flushes(monkeypatch):
    monkeypatch.setattr("backend.run_registry._FLUSH_SECONDS", 0.0)
    store = _CountingStore()
    reg = _registry(store)
    reg.create(_state())
    reg.update("abc123", lambda s: setattr(s.tables[0], "rows_copied", 10))
    reg.update("abc123", lambda s: setattr(s.tables[0], "rows_copied", 20))
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
    reg.update("abc123", lambda s: setattr(s.tables[0], "rows_copied", 10))
    monkeypatch.undo()
    reg.update("abc123", lambda s: setattr(s, "status", "success"))

    # Replaying the stale snapshot is dropped by the version guard.
    reg._persist(captured[0], 2)
    assert store.load("data_migration", "abc123")["status"] == "success"


def test_get_falls_back_to_the_store_for_another_workers_run():
    # Two registries over one store: the second never saw the run in memory.
    store = _CountingStore()
    owner = _registry(store)
    owner.create(_state(status="running"))
    owner.update("abc123", lambda s: setattr(s, "status", "success"))

    other_worker = _registry(store)
    recovered = other_worker.get("abc123")
    assert recovered is not None and recovered.status == "success"


def test_a_broken_store_never_breaks_a_run():
    reg = _registry(_BrokenStore())
    reg.create(_state())                                     # save raises
    reg.update("abc123", lambda s: setattr(s, "status", "success"))
    # Memory is still authoritative for the live run.
    assert reg.get("abc123").status == "success"
    assert reg.list() == []


def test_unreadable_stored_payload_reads_as_missing():
    store = _CountingStore()
    store.save("data_migration", "junk", "running", {"not": "a run state"})
    assert _registry(store).get("junk") is None


def test_list_is_scoped_to_the_registrys_kind():
    store = _CountingStore()
    _registry(store).create(_state("r1"))
    RunRegistry("validation", RunState, store=store).create(_state("r2"))
    assert [r.run_id for r in _registry(store).list()] == ["r1"]


# --- backend selection + history route ---------------------------------------


def test_store_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("LBX_RUNS_BACKEND", raising=False)
    monkeypatch.delenv("LBX_PROJECTS_BACKEND", raising=False)
    get_run_store.cache_clear()
    assert isinstance(get_run_store(), MemoryRunStore)
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
    store.save("data_migration", "r1", "success", {})
    monkeypatch.setattr(runs_routes, "get_run_store", lambda: store)

    result = runs_routes.history(kind=None, limit=50)
    assert result.persistent is False  # memory store — history dies with the process
    assert [r.run_id for r in result.runs] == ["r1"]
