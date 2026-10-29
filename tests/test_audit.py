"""Audit trail: who ran which migration step and touched which credential (backend/audit.py)."""
import json
import threading

import pglast
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend import audit
from backend.api import audit_routes, projects_routes
from backend.assessment.models import SecretRef
from backend.connectors import credentials
from backend.connectors.credential_store import MemoryCredentialStore
from backend.projects.store import LocalFileStore
from backend.run_registry import RunRegistry
from backend.run_store import MemoryRunStore


def _actions(store) -> list[str]:
    return [e.action for e in reversed(store.list())]


# --- Who: the actor ----------------------------------------------------------------


def _app_with_probe() -> FastAPI:
    from backend.main import bind_actor

    app = FastAPI()
    app.middleware("http")(bind_actor)

    @app.post("/probe")
    def probe() -> dict:
        audit.record("probe.called")
        return {"actor": audit.current_actor()}

    return app


def test_the_signed_in_user_is_the_actor(audit_in_memory):
    client = TestClient(_app_with_probe())
    body = client.post("/probe", headers={"X-Forwarded-Email": "ana@example.com"}).json()
    assert body["actor"] == "ana@example.com"
    assert audit_in_memory.list()[0].actor == "ana@example.com"


def test_without_a_user_the_app_identity_is_marked_as_such():
    body = TestClient(_app_with_probe()).post("/probe").json()
    assert body["actor"] == "test-app (app identity)"


def test_the_actor_does_not_leak_between_requests():
    client = TestClient(_app_with_probe())
    client.post("/probe", headers={"X-Forwarded-Email": "ana@example.com"})
    assert client.post("/probe").json()["actor"] == "test-app (app identity)"


def test_a_background_run_keeps_the_actor_that_started_it():
    seen: list[str] = []
    done = threading.Event()
    token = audit.set_actor("ana@example.com")
    try:
        audit.spawn(lambda: (seen.append(audit.current_actor()), done.set()))
    finally:
        audit.reset_actor(token)
    assert done.wait(5)
    assert seen == ["ana@example.com"]


# --- What: runs -------------------------------------------------------------------


class _State(BaseModel):
    run_id: str
    status: str = "running"


def test_a_run_is_recorded_when_it_starts_and_once_when_it_finishes(audit_in_memory):
    reg = RunRegistry("sync_run", _State, store=MemoryRunStore())
    reg.create(_State(run_id="r1"), project_id="p1")
    reg.update("r1", lambda s: setattr(s, "status", "success"))
    reg.update("r1", lambda s: None)  # a later write is not a second finish

    events = list(reversed(audit_in_memory.list()))
    assert [(e.action, e.outcome) for e in events] == [("run.started", "ok"),
                                                      ("run.finished", "success")]
    assert all(e.project_id == "p1" and e.target == "r1" for e in events)
    assert events[0].detail == {"kind": "sync_run", "status": "running"}


def test_a_repair_that_gives_up_has_finished(audit_in_memory):
    reg = RunRegistry("validation_repair", _State, store=MemoryRunStore())
    reg.create(_State(run_id="r1"))
    reg.update("r1", lambda s: setattr(s, "status", "gave_up"))
    assert audit_in_memory.list()[0].outcome == "gave_up"


# --- Credential access ------------------------------------------------------------


@pytest.fixture
def creds(monkeypatch):
    store = MemoryCredentialStore()
    monkeypatch.setattr(credentials, "get_credential_store", lambda: store)
    return store


def _resolve(typed: str = "", ref: SecretRef | None = None):
    return credentials.resolve_effective_password(
        "azure_sql", "src.example.com", "sales", "loader", "p1", typed, ref)


def test_reading_a_stored_password_is_recorded_without_the_password(creds, audit_in_memory):
    credentials.remember_password("azure_sql", "src.example.com", "sales", "loader",
                                  "s3cret!", "p1")
    assert _resolve() == ("s3cret!", None)

    stored, read = reversed(audit_in_memory.list())
    assert (stored.action, stored.detail) == ("credential.stored", {"kind": "password"})
    assert (read.action, read.detail) == ("credential.read", {"via": "stored_password"})
    assert read.target == "azure_sql:loader@src.example.com/sales" and read.project_id == "p1"
    assert "s3cret!" not in json.dumps([vars(e) for e in audit_in_memory.list()])


def test_a_typed_password_or_a_miss_reads_nothing(creds, audit_in_memory):
    assert _resolve(typed="typed")[0] == "typed"
    assert _resolve()[0] is None
    assert audit_in_memory.list() == []


def test_a_secret_reference_is_recorded_by_its_pointer(creds, audit_in_memory, monkeypatch):
    monkeypatch.setattr(credentials, "resolve_secret_ref", lambda ref: None)
    _resolve(ref=SecretRef(kind="databricks", scope="mig", key="src-pw"))

    event = audit_in_memory.list()[0]
    assert event.action == "credential.read" and event.outcome == "failed"
    assert event.detail == {"via": "secret_ref", "ref": "databricks:mig/src-pw"}


def test_clearing_a_projects_credentials_is_recorded(creds, audit_in_memory):
    credentials.clear_project("p1")
    assert _actions(audit_in_memory) == ["credential.cleared"]


# --- Projects ---------------------------------------------------------------------


def test_a_deleted_project_keeps_its_trail_and_its_name(tmp_path, monkeypatch, audit_in_memory):
    monkeypatch.setattr(projects_routes, "get_store", lambda: LocalFileStore(str(tmp_path)))
    monkeypatch.setattr(projects_routes, "clear_project_credentials", lambda pid: None)
    monkeypatch.setattr(projects_routes, "get_run_store", MemoryRunStore)
    app = FastAPI()
    app.include_router(projects_routes.router)
    client = TestClient(app)

    pid = client.post("/api/projects", json={"name": "Contoso ERP"}).json()["id"]
    client.delete(f"/api/projects/{pid}")

    events = list(reversed(audit_in_memory.list(project_id=pid)))
    assert [(e.action, e.target) for e in events] == [("project.created", "Contoso ERP"),
                                                     ("project.deleted", "Contoso ERP")]


# --- The store and the endpoint ---------------------------------------------------


def test_the_trail_filters_by_project_and_action_prefix():
    store = audit.MemoryAuditStore()
    for action, pid in (("run.started", "p1"), ("credential.read", "p1"),
                        ("run.finished", "p2"), ("runner.x", "p1")):
        store.append(audit.AuditEvent(action=action, actor="a", project_id=pid))
    assert [e.action for e in store.list(project_id="p1", action="run.")] == ["run.started"]
    assert [e.action for e in store.list(action="run.finished")] == ["run.finished"]
    assert [e.action for e in store.list(limit=2)] == ["runner.x", "run.finished"]


def test_a_broken_store_never_breaks_the_request(monkeypatch, caplog):
    class _Broken(audit.MemoryAuditStore):
        def append(self, event):
            raise RuntimeError("lakebase down")

    monkeypatch.setattr(audit, "get_audit_store", _Broken)
    audit.record("run.started")  # does not raise
    assert "lakebase down" in caplog.text


def test_the_endpoint_returns_the_trail_newest_first(audit_in_memory):
    audit.record("run.started", project_id="p1", target="r1")
    audit.record("run.finished", project_id="p1", target="r1", outcome="success")
    app = FastAPI()
    app.include_router(audit_routes.router)

    body = TestClient(app).get("/api/audit", params={"project_id": "p1"}).json()
    assert body["persistent"] is False
    assert [e["action"] for e in body["events"]] == ["run.finished", "run.started"]


# --- PostgresAuditStore (fake psycopg; no live DB) ---------------------------------


class _FakeCursor:
    def __init__(self, sql):
        self._sql = sql

    def execute(self, sql, params=None):
        self._sql.append((sql, params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sql):
        self._sql = sql

    def cursor(self, row_factory=None):
        return _FakeCursor(self._sql)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def pg(monkeypatch):
    import psycopg

    sql: list = []
    monkeypatch.setattr(psycopg, "connect", lambda **kw: _FakeConn(sql))
    store = audit.PostgresAuditStore(host="h", database="d", user="u", port=5432, password="p")
    return store, sql


def test_postgres_ddl_parses_and_guards_against_rewrites(pg):
    store, sql = pg
    store.append(audit.AuditEvent(action="run.started", actor="ana", at="2026-10-29T12:00:00+00:00"))
    ddl = [s for s, _ in sql if not s.startswith("INSERT")]
    for statement in ddl:
        pglast.parse_sql(statement)
    guards = " ".join(ddl)
    assert "BEFORE UPDATE OR DELETE" in guards and "BEFORE TRUNCATE" in guards


def test_postgres_store_appends_and_filters_by_prefix(pg):
    store, sql = pg
    store.append(audit.AuditEvent(action="credential.read", actor="ana", at="2026-10-29T12:00:00+00:00",
                                  project_id="p1", target="t", detail={"via": "stored_password"}))
    insert, params = sql[-1]
    assert insert.startswith('INSERT INTO "lbx_audit"')
    assert params[1:4] == ("ana", "credential.read", "p1")
    assert json.loads(params[-1]) == {"via": "stored_password"}

    store.list(project_id="p1", action="credential.")
    query, params = sql[-1]
    pglast.parse_sql(query.replace("%s", "NULL"))
    assert "starts_with(action, %s)" in query and params == ("p1", "credential.", 200)
