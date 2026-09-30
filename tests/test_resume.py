"""Resuming a data-migration run (backend/migration/runs.py).

A run that failed partway — or whose app restarted mid-load — used to force a full
re-copy: every table already in the target was truncated and streamed again. The
per-table state the run store keeps is now read back, so a resume loads only what
is left. Each table is copied in one transaction (data_loader), so "already
loaded" is exact rather than a guess.
"""
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import migration_routes
from backend.migration import runs
from backend.migration.models import (
    DataLoadRequest,
    LakebaseConnRequest,
    RunState,
    TableLoadSpec,
    TableProgress,
)
from backend.migration.runs import _execute as _real_execute
from backend.run_registry import RunRegistry
from backend.run_store import MemoryRunStore


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """A private store per test, so listing a project's runs sees only its own."""
    monkeypatch.setattr(
        runs, "_REGISTRY", RunRegistry("sync_run", RunState, store=MemoryRunStore())
    )


@pytest.fixture
def project() -> str:
    return str(uuid.uuid4())


def _req(project_id: str, tables=("dbo.Orders", "dbo.Items"), **over) -> DataLoadRequest:
    return DataLoadRequest(
        host="h", database="db", username="u", password="p",
        lakebase=LakebaseConnRequest(host="lb", database="d", user="u", password="p"),
        project_id=project_id,
        tables=[
            TableLoadSpec(schema_name=t.split(".")[0], table_name=t.split(".")[1], total_rows=10)
            for t in tables
        ],
        **over,
    )


def _seeded(monkeypatch, req: DataLoadRequest) -> str:
    """start_run's seeding, without the loader — the test drives that itself."""
    monkeypatch.setattr(runs, "_execute", lambda *a: None)
    return runs.start_run(req)


def _load_inline(monkeypatch, req, run_id, load_table, captured=(), restore=None) -> RunState:
    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: object())
    monkeypatch.setattr(runs, "capture_and_drop_fks", lambda *a: list(captured))
    monkeypatch.setattr(runs, "restore_fks", restore or (lambda *a: []))
    monkeypatch.setattr(runs, "load_table", load_table)
    runs._copy_tables(run_id, req)
    return runs.get_run(run_id)


def _sync(monkeypatch, req, load, **kw) -> RunState:
    """One whole run over fakes: seed, then copy inline."""
    return _load_inline(monkeypatch, req, _seeded(monkeypatch, req), load, **kw)


def _ok(rows: int = 5):
    return lambda *a, **k: rows


def _fails_on(table: str):
    def load(_source, _target, spec, *a, **k):
        if spec.table_name == table:
            raise RuntimeError(f"{table} blew up")
        return 5
    return load


def _records(sink: list, rows: int = 7):
    def load(_source, _target, spec, *a, **k):
        sink.append(spec.table_name)
        return rows
    return load


# --- Skipping what is already loaded ---------------------------------------------


def test_a_resumed_run_copies_only_what_is_left(monkeypatch, project):
    first = _sync(monkeypatch, _req(project), _fails_on("Items"))
    assert [t.status for t in first.tables] == ["success", "failed"]

    copied: list[str] = []
    second = _sync(monkeypatch, _req(project, resume_from=first.run_id), _records(copied))

    assert copied == ["Items"], "the table already in the target was re-copied"
    assert [t.status for t in second.tables] == ["skipped", "success"]
    assert second.status == "success"


def test_a_skipped_table_keeps_the_row_count_and_timings_it_was_loaded_with(monkeypatch, project):
    first = _sync(monkeypatch, _req(project), _fails_on("Items"))
    done = first.tables[0]

    second = _sync(monkeypatch, _req(project, resume_from=first.run_id), _ok())
    skipped = second.tables[0]

    # Carried over, so the run's rows and per-table history still add up.
    assert skipped.rows_copied == done.rows_copied
    assert skipped.started_at == done.started_at
    assert skipped.finished_at == done.finished_at


def test_a_resumed_run_records_the_run_it_resumed(monkeypatch, project):
    first = _sync(monkeypatch, _req(project), _fails_on("Items"))
    second = _sync(monkeypatch, _req(project, resume_from=first.run_id), _ok())

    assert second.resumed_from == first.run_id
    assert second.run_id != first.run_id, "a resume is its own run, not a rewrite"


def test_a_run_that_resumes_nothing_copies_every_table(monkeypatch, project):
    copied: list[str] = []
    state = _sync(monkeypatch, _req(project), _records(copied))

    assert copied == ["Orders", "Items"]
    assert state.resumed_from is None


def test_a_table_selected_since_the_last_run_is_copied_by_the_resume(monkeypatch, project):
    first = _sync(monkeypatch, _req(project, tables=("dbo.Orders",)), _ok())

    copied: list[str] = []
    req = _req(project, tables=("dbo.Orders", "dbo.Items"), resume_from=first.run_id)
    second = _sync(monkeypatch, req, _records(copied))

    assert copied == ["Items"]
    assert [t.status for t in second.tables] == ["skipped", "success"]


def test_a_table_that_failed_twice_stays_failed(monkeypatch, project):
    first = _sync(monkeypatch, _req(project), _fails_on("Items"))
    second = _sync(monkeypatch, _req(project, resume_from=first.run_id), _fails_on("Items"))

    assert [t.status for t in second.tables] == ["skipped", "failed"]
    assert second.status == "partial"
    assert runs.is_resumable(second)


# --- Foreign keys an interrupted run left dropped --------------------------------
#
# capture_and_drop_fks drops every FK touching the targets and only restore_fks puts
# them back. The definitions used to live in a local variable, so a run that died
# mid-load took the only copy with it and left the target with no FKs at all.

_FK = ("public.orders", "fk_orders_customer", "FOREIGN KEY (cid) REFERENCES public.customers(id)")
_FK2 = ("public.items", "fk_items_order", "FOREIGN KEY (oid) REFERENCES public.orders(id)")


def test_the_definitions_are_persisted_before_the_load_starts(monkeypatch, project):
    """What the run knows mid-load is all a resume will ever have."""
    req = _req(project)
    run_id = _seeded(monkeypatch, req)
    seen: list[list] = []

    def load(*a, **k):
        seen.append(list(runs.get_run(run_id).dropped_fks))
        return 5

    _load_inline(monkeypatch, req, run_id, load, captured=[_FK])

    assert seen[0] == [_FK]


def test_the_foreign_keys_an_interrupted_run_dropped_are_restored_by_the_resume(monkeypatch, project):
    # A run that dies mid-load never reaches restore_fks, so its state keeps them.
    req = _req(project)
    run_id = _seeded(monkeypatch, req)
    runs._set(run_id, lambda s: setattr(s, "dropped_fks", [_FK]))
    interrupted = runs.get_run(run_id)

    restored: list[list] = []
    # Nothing left to capture — the interrupted run already dropped them.
    _sync(
        monkeypatch, _req(project, resume_from=interrupted.run_id), _ok(),
        captured=[], restore=lambda _t, dropped: restored.append(list(dropped)) or [],
    )

    assert restored == [[_FK]]


def test_a_freshly_captured_definition_wins_over_the_inherited_one(monkeypatch):
    changed = (_FK[0], _FK[1], "FOREIGN KEY (cid) REFERENCES public.customers(id) ON DELETE CASCADE")

    merged = runs._merge_fks([_FK, _FK2], [changed])

    assert sorted(merged) == sorted([changed, _FK2])


def test_restoring_clears_the_bookkeeping_so_a_later_resume_does_not_retry(monkeypatch, project):
    state = _sync(monkeypatch, _req(project), _ok(), captured=[_FK])

    assert state.dropped_fks == []


def test_a_reported_restore_failure_also_clears_it(monkeypatch, project):
    """The plan's idempotent FK items are the backstop; retrying orphan rows is not."""
    state = _sync(
        monkeypatch, _req(project), _ok(), captured=[_FK],
        restore=lambda *a: ["public.orders fk_orders_customer: orphan rows"],
    )

    assert state.dropped_fks == []
    assert state.status == "partial"
    assert "could not be restored" in state.error


# --- Which runs may be resumed ---------------------------------------------------


def _state(status: str, table_statuses=("success", "failed"), **over) -> RunState:
    fields = dict(
        run_id=str(uuid.uuid4()),
        status=status,
        started_at=runs._now(),
        heartbeat_at=runs._now(),
        tables=[
            TableProgress(name=f"dbo.t{i}", target=f"public.t{i}", status=s)
            for i, s in enumerate(table_statuses)
        ],
    )
    return RunState(**{**fields, **over})


def test_a_live_run_is_not_resumable():
    """Two loaders on one table would fight over the same TRUNCATE."""
    assert not runs.is_resumable(_state("running", ("success", "running")))


def test_a_run_whose_heartbeat_stopped_is_resumable():
    stale = _state("running", ("success", "running"))
    stale.heartbeat_at = _ago(runs.STALE_AFTER_SECONDS + 10)

    assert runs.is_resumable(stale)


def test_a_run_from_before_heartbeats_falls_back_to_when_it_started():
    legacy = _state("running", ("success", "pending"), heartbeat_at=None)
    legacy.started_at = _ago(runs.STALE_AFTER_SECONDS + 10)

    assert runs.is_resumable(legacy)


def test_a_finished_run_has_nothing_to_resume():
    assert not runs.is_resumable(_state("success", ("success", "success")))


def test_a_partial_run_is_resumable():
    assert runs.is_resumable(_state("partial", ("success", "failed")))


def test_a_run_that_loaded_everything_but_left_foreign_keys_dropped_is_resumable():
    """Nothing to copy, but the target is still missing its FKs."""
    state = _state("failed", ("success", "success"), dropped_fks=[_FK])

    assert runs.is_resumable(state)


def test_a_resumed_run_does_not_count_skipped_tables_as_work_left():
    assert runs.tables_left(_state("partial", ("skipped", "success"))) == 0
    assert runs.tables_left(_state("partial", ("skipped", "pending"))) == 1


def _ago(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# --- Finding the run to offer ----------------------------------------------------


def test_the_newest_resumable_run_of_the_project_is_offered(monkeypatch, project):
    _sync(monkeypatch, _req(project), _ok())                      # finished: nothing left
    older = _sync(monkeypatch, _req(project), _fails_on("Items"))
    newer = _sync(monkeypatch, _req(project), _fails_on("Items"))

    found = runs.find_resumable(project)

    assert found.run_id == newer.run_id != older.run_id


def test_a_run_that_was_already_resumed_is_not_offered_again(monkeypatch, project):
    """What it left behind is the resume's business now — offering it again would
    re-copy tables that run has already loaded."""
    failed = _sync(monkeypatch, _req(project), _fails_on("Items"))
    _sync(monkeypatch, _req(project, resume_from=failed.run_id), _ok())

    assert runs.find_resumable(project) is None


def test_a_resume_that_failed_too_is_offered_not_its_ancestor(monkeypatch, project):
    first = _sync(monkeypatch, _req(project), _fails_on("Items"))
    second = _sync(monkeypatch, _req(project, resume_from=first.run_id), _fails_on("Items"))

    assert runs.find_resumable(project).run_id == second.run_id


def test_another_projects_run_is_never_offered(monkeypatch, project):
    _sync(monkeypatch, _req(str(uuid.uuid4())), _fails_on("Items"))

    assert runs.find_resumable(project) is None


def test_nothing_is_offered_when_every_run_finished(monkeypatch, project):
    _sync(monkeypatch, _req(project), _ok())

    assert runs.find_resumable(project) is None


def test_the_resumable_endpoint_reports_what_is_left(monkeypatch, project):
    failed = _sync(monkeypatch, _req(project), _fails_on("Items"))

    body = migration_routes.resumable_data(project)

    assert body.run.run_id == failed.run_id
    assert body.run.status == "partial"
    assert (body.run.tables_total, body.run.tables_left) == (2, 1)
    assert body.run.rows_copied == 5


def test_the_resumable_endpoint_says_nothing_when_there_is_nothing(monkeypatch, project):
    assert migration_routes.resumable_data(project).run is None


# --- The route's guards ----------------------------------------------------------


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """The real start_data — only the background loader is stubbed out."""
    monkeypatch.setattr(runs, "_execute", lambda *a: None)
    app = FastAPI()
    app.include_router(migration_routes.router)
    return TestClient(app)


def _start_body(project_id: str, resume_from: str) -> dict:
    return {
        "host": "h", "database": "db", "username": "u", "password": "p",
        "lakebase": {"host": "lb", "database": "d", "user": "u", "password": "p"},
        "project_id": project_id,
        "tables": [{"schema_name": "dbo", "table_name": "Orders"}],
        "resume_from": resume_from,
    }


def test_resuming_an_unknown_run_is_a_404(client, project):
    r = client.post("/api/migration/data/start", json=_start_body(project, str(uuid.uuid4())))

    assert r.status_code == 404
    assert "Unknown run id" in r.json()["detail"]


def test_resuming_a_live_run_is_refused(client, monkeypatch, project):
    live = _seeded(monkeypatch, _req(project))

    r = client.post("/api/migration/data/start", json=_start_body(project, live))

    assert r.status_code == 409
    assert "still active" in r.json()["detail"]


def test_resuming_another_projects_run_is_refused(client, monkeypatch, project):
    theirs = _sync(monkeypatch, _req(str(uuid.uuid4())), _fails_on("Items"))

    r = client.post("/api/migration/data/start", json=_start_body(project, theirs.run_id))

    assert r.status_code == 409
    assert "another migration project" in r.json()["detail"]


def test_a_resumable_run_starts_and_is_seeded_from_the_one_it_resumes(client, monkeypatch, project):
    failed = _sync(monkeypatch, _req(project), _fails_on("Items"))

    r = client.post("/api/migration/data/start", json=_start_body(project, failed.run_id))

    assert r.status_code == 200
    resumed = runs.get_run(r.json()["run_id"])
    assert resumed.resumed_from == failed.run_id
    # The body asks only for the table the failed run had already loaded.
    assert [t.status for t in resumed.tables] == ["skipped"]


# --- Heartbeat -------------------------------------------------------------------


def test_the_loader_heartbeats_while_it_copies(monkeypatch, project):
    """Without proof of life, a run stuck on one slow table is indistinguishable
    from a run whose app is gone — and resuming a live one would be unsafe."""
    monkeypatch.setattr(runs, "HEARTBEAT_SECONDS", 0.01)
    req = _req(project)
    run_id = _seeded(monkeypatch, req)
    before = runs.get_run(run_id).heartbeat_at

    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: object())
    monkeypatch.setattr(runs, "capture_and_drop_fks", lambda *a: [])
    monkeypatch.setattr(runs, "restore_fks", lambda *a: [])
    monkeypatch.setattr(runs, "load_table", lambda *a, **k: time.sleep(0.1) or 5)
    _real_execute(run_id, req)

    assert runs.get_run(run_id).heartbeat_at > before


def test_the_heartbeat_stops_with_the_run(monkeypatch, project):
    monkeypatch.setattr(runs, "HEARTBEAT_SECONDS", 0.01)
    req = _req(project)
    run_id = _seeded(monkeypatch, req)
    live = threading.active_count()

    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: object())
    monkeypatch.setattr(runs, "capture_and_drop_fks", lambda *a: [])
    monkeypatch.setattr(runs, "restore_fks", lambda *a: [])
    monkeypatch.setattr(runs, "load_table", _ok())
    _real_execute(run_id, req)
    time.sleep(0.05)

    assert threading.active_count() <= live
