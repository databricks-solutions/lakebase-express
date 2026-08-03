"""Session credential cache + assessment routes falling back to it.

The SPA holds passwords only in browser session state, so a page reload sends
requests with an empty password. The backend must fall back to the password
that last authenticated for the same connection instead of failing the scan.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import assessment_routes
from backend.assessment.models import AssessmentReport
from backend.connectors import credentials


@pytest.fixture(autouse=True)
def clean_cache():
    credentials.clear()
    yield
    credentials.clear()


# --- cache unit tests --------------------------------------------------------


def test_remember_and_resolve_round_trip():
    credentials.remember_password("azure-sql", "Srv.Database.Windows.Net", "db", "u@srv", "pw")
    # Host lookup is case-insensitive; database/username are verbatim.
    assert credentials.resolve_password("azure-sql", "srv.database.windows.net", "db", "u@srv") == "pw"
    assert credentials.resolve_password("azure-sql", "srv.database.windows.net", "other", "u@srv") is None


def test_empty_password_is_never_cached():
    credentials.remember_password("azure-sql", "srv", "db", "u", "")
    assert credentials.resolve_password("azure-sql", "srv", "db", "u") is None


def test_newer_password_replaces_older():
    credentials.remember_password("azure-sql", "srv", "db", "u", "old")
    credentials.remember_password("azure-sql", "srv", "db", "u", "new")
    assert credentials.resolve_password("azure-sql", "srv", "db", "u") == "new"


def test_credentials_are_scoped_per_project():
    # Same host/db/user in two projects must not share a stored password.
    credentials.remember_password("azure-sql", "srv", "db", "u", "pw-A", project_id="proj-A")
    credentials.remember_password("azure-sql", "srv", "db", "u", "pw-B", project_id="proj-B")
    assert credentials.resolve_password("azure-sql", "srv", "db", "u", project_id="proj-A") == "pw-A"
    assert credentials.resolve_password("azure-sql", "srv", "db", "u", project_id="proj-B") == "pw-B"
    # A project-agnostic lookup (no id) is its own bucket, not either project's.
    assert credentials.resolve_password("azure-sql", "srv", "db", "u") is None


# --- route integration -------------------------------------------------------


_REQ = {
    "source_type": "azure-sql",
    "host": "srv.database.windows.net",
    "database": "db",
    "username": "u@srv",
    "port": 1433,
}


def _report() -> AssessmentReport:
    return AssessmentReport(
        database="db", table_count=0, total_rows=0, programmable_object_count=0,
        findings=[], readiness_score=100, severity_counts={}, tables=[],
        programmable_objects=[],
    )


@pytest.fixture
def client(monkeypatch):
    seen: dict = {}

    def fake_run_assessment(conn, *, use_ai=True, endpoint=None):
        seen["password"] = conn.password
        return _report()

    monkeypatch.setattr(assessment_routes, "run_assessment", fake_run_assessment)
    monkeypatch.setattr(
        "backend.connectors.azure_sql.AzureSqlConnection.test_connection", lambda self: True
    )
    app = FastAPI()
    app.include_router(assessment_routes.router)
    return TestClient(app), seen


def test_scan_falls_back_to_cached_password(client):
    c, seen = client
    # A successful test-connection caches the password for the session…
    r = c.post("/api/assessment/test-connection", json={**_REQ, "password": "pw1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # …so a scan arriving with an empty password (post page-reload) still works.
    r = c.post("/api/assessment/scan", json={**_REQ, "password": ""})
    assert r.status_code == 200
    assert seen["password"] == "pw1"


def test_scan_with_explicit_password_wins_and_is_cached(client):
    c, seen = client
    credentials.remember_password("azure-sql", _REQ["host"], "db", "u@srv", "stale")
    r = c.post("/api/assessment/scan", json={**_REQ, "password": "fresh"})
    assert r.status_code == 200
    assert seen["password"] == "fresh"
    # A successful scan refreshes the cache too.
    assert credentials.resolve_password("azure-sql", _REQ["host"], "db", "u@srv") == "fresh"


def test_scan_without_password_or_cache_is_a_clear_400(client):
    c, _ = client
    r = c.post("/api/assessment/scan", json={**_REQ, "password": ""})
    assert r.status_code == 400
    assert "re-enter" in r.json()["detail"].lower()


def test_scan_credentials_do_not_leak_across_projects(client):
    c, seen = client
    # Project A authenticates and caches under its own id…
    r = c.post("/api/assessment/scan", json={**_REQ, "password": "pwA", "project_id": "A"})
    assert r.status_code == 200 and seen["password"] == "pwA"
    # …so project B (same connection, empty password) can't reuse it and gets a 400.
    r = c.post("/api/assessment/scan", json={**_REQ, "password": "", "project_id": "B"})
    assert r.status_code == 400
    # But project A resolves its own cached password on a reload.
    r = c.post("/api/assessment/scan", json={**_REQ, "password": "", "project_id": "A"})
    assert r.status_code == 200 and seen["password"] == "pwA"


def test_failed_scan_does_not_cache_password(client, monkeypatch):
    c, _ = client

    def boom(conn, *, use_ai=True, endpoint=None):
        raise RuntimeError("login failed")

    monkeypatch.setattr(assessment_routes, "run_assessment", boom)
    r = c.post("/api/assessment/scan", json={**_REQ, "password": "wrong"})
    assert r.status_code == 502
    assert credentials.resolve_password("azure-sql", _REQ["host"], "db", "u@srv") is None
