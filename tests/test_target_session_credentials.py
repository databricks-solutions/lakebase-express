"""Target (Lakebase) credentials falling back to the session cache.

Mirrors the source-side behavior in test_session_credentials.py: the SPA loses
passwords on any page reload, so the migration endpoints must resolve an empty
Lakebase password from the last one that authenticated successfully — instead of
the Create Sync step failing with "Missing Lakebase password".
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import migration_routes
from backend.connectors import credentials
from backend.migration.models import ItemResult  # noqa: F401  (re-exported shapes)


@pytest.fixture(autouse=True)
def clean_cache():
    credentials.clear()
    yield
    credentials.clear()


_LB = {
    "host": "ep-x.database.azuredatabricks.net",
    "database": "xpmig",
    "user": "lbx_app",
    "port": 5432,
    "sslmode": "require",
}

_PLAN_ITEM = {"id": "1", "kind": "schema", "name": "clients", "sql": "CREATE SCHEMA clients"}


class _FakeLakebase:
    """Stands in for LakebaseConnection; records the password it was built with."""

    last_password: str | None = None
    test_ok = True

    def __init__(self, **kwargs):
        _FakeLakebase.last_password = kwargs.get("password")

    def test_connection(self):
        return _FakeLakebase.test_ok


@pytest.fixture
def client(monkeypatch):
    _FakeLakebase.last_password = None
    _FakeLakebase.test_ok = True
    monkeypatch.setattr(migration_routes, "LakebaseConnection", _FakeLakebase)
    monkeypatch.setattr(migration_routes, "apply_plan", lambda conn, items, stop: [])
    app = FastAPI()
    app.include_router(migration_routes.router)
    return TestClient(app)


def _prime_cache():
    credentials.remember_password("lakebase", _LB["host"], _LB["database"], _LB["user"], "pgpw")


# --- /lakebase/test ----------------------------------------------------------


def test_successful_test_remembers_password(client):
    r = client.post("/api/migration/lakebase/test", json={**_LB, "password": "pgpw"})
    assert r.json()["ok"] is True
    assert credentials.resolve_password("lakebase", _LB["host"], _LB["database"], _LB["user"]) == "pgpw"


def test_failed_test_does_not_remember(client):
    _FakeLakebase.test_ok = False
    r = client.post("/api/migration/lakebase/test", json={**_LB, "password": "wrong"})
    assert r.json()["ok"] is False
    assert credentials.resolve_password("lakebase", _LB["host"], _LB["database"], _LB["user"]) is None


def test_test_with_empty_password_uses_cache(client):
    _prime_cache()
    r = client.post("/api/migration/lakebase/test", json={**_LB, "password": ""})
    assert r.json()["ok"] is True
    assert _FakeLakebase.last_password == "pgpw"


def test_test_with_no_password_and_no_cache_is_friendly(client):
    r = client.post("/api/migration/lakebase/test", json=_LB)  # password omitted entirely
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False
    assert "re-enter" in body["message"].lower()


# --- /apply ------------------------------------------------------------------


def test_apply_falls_back_to_cached_password(client):
    _prime_cache()
    r = client.post(
        "/api/migration/apply",
        json={"lakebase": {**_LB, "password": ""}, "items": [_PLAN_ITEM]},
    )
    assert r.status_code == 200
    assert _FakeLakebase.last_password == "pgpw"


def test_apply_without_password_or_cache_is_a_clear_400(client):
    r = client.post(
        "/api/migration/apply",
        json={"lakebase": {**_LB, "password": ""}, "items": [_PLAN_ITEM]},
    )
    assert r.status_code == 400
    assert "re-enter" in r.json()["detail"].lower()


def test_apply_with_explicit_password_is_remembered(client):
    r = client.post(
        "/api/migration/apply",
        json={"lakebase": {**_LB, "password": "fresh"}, "items": [_PLAN_ITEM]},
    )
    assert r.status_code == 200
    assert credentials.resolve_password("lakebase", _LB["host"], _LB["database"], _LB["user"]) == "fresh"


# The repair agent moved to the Post-Migration Validation module — its
# session-cache fallback is covered in tests/test_validation_credentials.py.


# --- /data/start -------------------------------------------------------------


_SOURCE = {
    "source_type": "azure-sql",
    "host": "srv.database.windows.net",
    "database": "xptoy",
    "username": "sqladmin@srv",
    "port": 1433,
}
_TABLE = {"schema_name": "clients", "table_name": "Account"}


def test_data_start_resolves_both_sides_from_cache(client, monkeypatch):
    seen = {}

    def fake_start_run(req):
        seen["source"] = req.password
        seen["target"] = req.lakebase.password
        return "run-2"

    monkeypatch.setattr(migration_routes.runs, "start_run", fake_start_run)
    credentials.remember_password(
        _SOURCE["source_type"], _SOURCE["host"], _SOURCE["database"], _SOURCE["username"], "srcpw"
    )
    _prime_cache()
    r = client.post(
        "/api/migration/data/start",
        json={
            **_SOURCE, "password": "",
            "lakebase": {**_LB, "password": ""},
            "tables": [_TABLE],
        },
    )
    assert r.status_code == 200 and r.json()["run_id"] == "run-2"
    assert seen == {"source": "srcpw", "target": "pgpw"}


def test_data_start_accepts_a_ref_without_password_placeholder(client, monkeypatch):
    seen = {}

    monkeypatch.setattr(credentials, "resolve_secret_ref", lambda ref: "src-from-ref")

    def fake_start_run(req):
        seen["password"] = req.password
        return "run-ref"

    monkeypatch.setattr(migration_routes.runs, "start_run", fake_start_run)
    _prime_cache()
    r = client.post(
        "/api/migration/data/start",
        json={
            **_SOURCE,
            "secret_ref": {
                "workspace_host": "adb-123.example.net",
                "scope": "source-scope",
                "key": "sql-password",
            },
            "lakebase": {**_LB, "password": ""},
            "tables": [_TABLE],
        },
    )
    assert r.status_code == 200 and r.json()["run_id"] == "run-ref"
    assert seen["password"] == "src-from-ref"


def test_data_start_without_source_password_is_a_clear_400(client):
    _prime_cache()  # target cached, source not
    r = client.post(
        "/api/migration/data/start",
        json={
            **_SOURCE, "password": "",
            "lakebase": {**_LB, "password": ""},
            "tables": [_TABLE],
        },
    )
    assert r.status_code == 400
    assert "source password" in r.json()["detail"].lower()


# --- /secrets/ensure resolving blanks from the credential store --------------


@pytest.fixture
def secrets_client(client, monkeypatch):
    """The migration client plus a stub secret-scope writer that records the
    values it was asked to write (so we can assert what got resolved)."""
    written: dict = {}

    def fake_ensure(scope, secrets):
        written.clear()
        written.update({k: v for k, v in secrets.items() if v})
        return {"scope": scope, "created": True, "keys": sorted(written)}

    monkeypatch.setattr(migration_routes.secret_setup, "ensure_secret_scope", fake_ensure)
    return client, written


def test_ensure_secrets_fills_blank_lakebase_value_from_store(secrets_client):
    c, written = secrets_client
    _prime_cache()  # target password stored under project-agnostic key
    r = c.post("/api/migration/secrets/ensure", json={
        "scope": "sc",
        "secrets": {},  # nothing entered this session
        "lakebase": {**_LB, "password": ""},
        "lakebase_key": "lakebase-password",
    })
    assert r.status_code == 200
    # The blank value was resolved from the store and written to the scope.
    assert written == {"lakebase-password": "pgpw"}


def test_ensure_secrets_prefers_supplied_value_over_store(secrets_client):
    c, written = secrets_client
    _prime_cache()  # store has "pgpw"
    r = c.post("/api/migration/secrets/ensure", json={
        "scope": "sc",
        "secrets": {"lakebase-password": "entered-now"},
        "lakebase": {**_LB, "password": ""},
        "lakebase_key": "lakebase-password",
    })
    assert r.status_code == 200
    assert written == {"lakebase-password": "entered-now"}  # supplied wins, no store lookup


def test_ensure_secrets_omits_key_when_neither_supplied_nor_stored(secrets_client):
    c, written = secrets_client  # nothing primed
    r = c.post("/api/migration/secrets/ensure", json={
        "scope": "sc",
        "secrets": {},
        "lakebase": {**_LB, "password": ""},
        "lakebase_key": "lakebase-password",
    })
    assert r.status_code == 200
    assert "lakebase-password" not in written  # can't resolve → not written
