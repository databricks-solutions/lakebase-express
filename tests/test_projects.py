"""Project store + system-object exclusion."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import projects_routes
from backend.assessment import scanner
from backend.assessment.models import SecretRef
from backend.connectors.credential_store import MemoryCredentialStore
from backend.projects.models import PhaseStatus, Project
from backend.projects.store import LocalFileStore, PostgresStore

# The Postgres store keys on a native uuid column, so ids must be valid UUIDs.
_UUID_A = "11111111-1111-4111-8111-111111111111"
_UUID_B = "22222222-2222-4222-8222-222222222222"


def _project(pid: str, name: str) -> Project:
    return Project(id=pid, name=name, created_at="2026-01-01T00:00:00+00:00",
                   updated_at="2026-01-01T00:00:00+00:00")


def test_local_store_round_trip(tmp_path):
    store = LocalFileStore(str(tmp_path))
    assert store.list() == []

    store.save(_project("abc123", "Sales DB"))
    store.save(_project("def456", "HR DB"))

    summaries = store.list()
    assert {s.id for s in summaries} == {"abc123", "def456"}
    assert {s.name for s in summaries} == {"Sales DB", "HR DB"}

    loaded = store.get("abc123")
    assert loaded and loaded.name == "Sales DB"
    assert loaded.statuses["assessment"] is PhaseStatus.NOT_STARTED
    assert loaded.identifier_case.value == "lowercase"

    store.delete("abc123")
    assert store.get("abc123") is None
    assert {s.id for s in store.list()} == {"def456"}


def test_local_store_preserves_workspace_bound_secret_refs(tmp_path):
    project = _project("abc123", "Sales DB")
    project.source.secret_ref = SecretRef(
        workspace_host="adb-123.example.net", scope="source-scope", key="sql-password"
    )
    project.target.secret_ref = SecretRef(
        workspace_host="adb-123.example.net", scope="target-scope", key="pg-password"
    )
    store = LocalFileStore(str(tmp_path))
    store.save(project)

    loaded = store.get(project.id)
    assert loaded is not None
    assert loaded.source.secret_ref == project.source.secret_ref
    assert loaded.target.secret_ref == project.target.secret_ref


def test_store_ignores_corrupt_files(tmp_path):
    (tmp_path / "garbage.json").write_text("not json")
    assert LocalFileStore(str(tmp_path)).list() == []


# --- Project DELETE also owns project-scoped credential cleanup ------------------


def _projects_client(monkeypatch, store, clear_credentials):
    monkeypatch.setattr(projects_routes, "get_store", lambda: store)
    monkeypatch.setattr(projects_routes, "clear_project_credentials", clear_credentials)
    app = FastAPI()
    app.include_router(projects_routes.router)
    return TestClient(app)


def test_delete_project_removes_only_its_credentials(tmp_path, monkeypatch):
    projects = LocalFileStore(str(tmp_path))
    projects.save(_project("project-one", "One"))
    projects.save(_project("project-two", "Two"))

    credentials = MemoryCredentialStore()
    one = ("project-one", "azure-sql", "one.example", "db", "user")
    two = ("project-two", "azure-sql", "two.example", "db", "user")
    credentials.remember(one, "pw-one")
    credentials.remember(two, "pw-two")

    client = _projects_client(monkeypatch, projects, credentials.clear_project)
    response = client.delete("/api/projects/project-one")

    assert response.status_code == 200 and response.json() == {"ok": True}
    assert projects.get("project-one") is None
    assert projects.get("project-two") is not None
    assert credentials.resolve(one) is None
    assert credentials.resolve(two) == "pw-two"


def test_delete_project_keeps_project_when_credential_cleanup_fails(tmp_path, monkeypatch):
    projects = LocalFileStore(str(tmp_path))
    projects.save(_project("project-one", "One"))

    def fail_cleanup(project_id):
        raise RuntimeError(f"credential store unavailable for {project_id}")

    client = _projects_client(monkeypatch, projects, fail_cleanup)
    response = client.delete("/api/projects/project-one")

    assert response.status_code == 502
    assert projects.get("project-one") is not None
    assert "stored credentials could not be removed" in response.json()["detail"]


def test_identifier_case_change_invalidates_name_dependent_artifacts(tmp_path, monkeypatch):
    projects = LocalFileStore(str(tmp_path))
    project = _project("project-one", "One")
    project.plan = [{"id": "table:dbo.Orders", "sql": 'CREATE TABLE "public"."orders"'}]
    project.validation = {"match_score": 100}
    projects.save(project)

    client = _projects_client(monkeypatch, projects, lambda _project_id: None)
    payload = project.model_dump(mode="json")
    payload["identifier_case"] = "preserve"
    response = client.put("/api/projects/project-one", json=payload)

    assert response.status_code == 200
    saved = projects.get("project-one")
    assert saved is not None and saved.identifier_case.value == "preserve"
    assert saved.plan is None and saved.validation is None


# --- PostgresStore (fake psycopg connection; no live DB) --------------------------

class _FakeCursor:
    """Minimal psycopg-cursor stand-in over a shared dict {id: data-dict}, enough
    for the store's CREATE TABLE / SELECT / UPSERT / DELETE statements."""

    def __init__(self, rows, dict_row=False):
        self._rows = rows
        self._dict_row = dict_row
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE"):
            self._result = []
        elif s.startswith("SELECT data FROM") and "WHERE id" in s:
            row = self._rows.get(params[0])
            self._result = [{"data": row} if self._dict_row else (row,)] if row is not None else []
        elif s.startswith("SELECT data FROM"):
            self._result = [{"data": r} if self._dict_row else (r,) for r in self._rows.values()]
        elif s.startswith("INSERT INTO"):
            pid, payload = params
            self._rows[pid] = json.loads(payload)
            self._result = []
        elif s.startswith("DELETE FROM"):
            self._rows.pop(params[0], None)
            self._result = []
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, row_factory=None):
        return _FakeCursor(self._rows, dict_row=row_factory is not None)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _pg_store():
    store = PostgresStore(host="h", database="databricks_postgres", user="u", port=5432,
                          password="p")
    rows: dict = {}
    store._connect = lambda: _FakeConn(rows)  # type: ignore[method-assign]
    return store, rows


def test_postgres_store_round_trip():
    store, _ = _pg_store()
    assert store.list() == []

    store.save(_project(_UUID_A, "Sales DB"))
    store.save(_project(_UUID_B, "HR DB"))

    assert {s.id for s in store.list()} == {_UUID_A, _UUID_B}
    loaded = store.get(_UUID_A)
    assert loaded and loaded.name == "Sales DB"
    assert loaded.statuses["assessment"] is PhaseStatus.NOT_STARTED

    store.delete(_UUID_A)
    assert store.get(_UUID_A) is None
    assert {s.id for s in store.list()} == {_UUID_B}


def test_postgres_store_upserts_on_save():
    store, rows = _pg_store()
    store.save(_project(_UUID_A, "First"))
    store.save(_project(_UUID_A, "Renamed"))
    assert len(rows) == 1
    assert store.get(_UUID_A).name == "Renamed"


def test_postgres_store_rejects_non_uuid_id():
    # A malformed id must not reach the ::uuid cast — save raises, get/delete miss.
    store, _ = _pg_store()
    try:
        store.save(_project("not-a-uuid", "Bad"))
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert store.get("not-a-uuid") is None
    store.delete("not-a-uuid")  # no-op, no raise


def test_postgres_store_preserves_plan_round_trip():
    # The whole point: the plan (incl. post-data items) must survive persistence.
    p = _project(_UUID_A, "toydata")
    p.plan = [
        {"id": "table:dbo.Orders", "kind": "table", "name": "public.orders", "sql": "CREATE TABLE ..."},
        {"id": "pk:dbo.Orders", "kind": "constraint", "name": "public.orders · PRIMARY KEY", "sql": "ALTER ..."},
        {"id": "fk:dbo.Orders.fk1", "kind": "foreign_key", "name": "public.orders · fk1", "sql": "DO $$ ..."},
    ]
    store, _ = _pg_store()
    store.save(p)
    loaded = store.get(_UUID_A)
    assert loaded is not None
    assert [i["kind"] for i in loaded.plan] == ["table", "constraint", "foreign_key"]


def test_postgres_store_ignores_corrupt_rows():
    store, rows = _pg_store()
    rows[_UUID_A] = _project(_UUID_A, "ok").model_dump(mode="json")
    rows["bad"] = {"not": "a project"}
    assert {s.id for s in store.list()} == {_UUID_A}


def test_scanner_excludes_system_objects():
    # The catalog queries must filter system schemas and MS-shipped objects so
    # system views/procs are never scanned or translated.
    for sql in (scanner._MODULES_SQL, scanner._ROWCOUNTS_SQL, scanner._COLUMNS_SQL):
        assert "is_ms_shipped = 0" in sql
        assert "'sys'" in sql
        assert "'INFORMATION_SCHEMA'" in sql
