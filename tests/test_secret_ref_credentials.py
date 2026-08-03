"""Password-source references: resolve a connection password from a Databricks
secret / Azure Key Vault-backed scope instead of typing it, for both the source
and Lakebase target. Covers the resolution precedence, the persist-the-pointer
(never the value) invariant, and the assessment route end to end.
"""
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import assessment_routes
from backend.assessment.models import AssessmentReport, SecretRef
from backend.connectors import credentials


@pytest.fixture(autouse=True)
def clean_cache():
    credentials.clear()
    yield
    credentials.clear()


@pytest.fixture(autouse=True)
def stub_secret_read(monkeypatch):
    """Stand in for the Databricks Secrets API: map (scope, key) -> value."""
    vault = {
        ("db-scope", "sql-pw"): "from-databricks",
        ("kv-scope", "vault-secret"): "from-keyvault",
    }
    monkeypatch.setattr(
        credentials, "resolve_secret_ref",
        lambda ref: vault.get((ref.scope, ref.key)),
    )


_ST, _H, _D, _U, _P = "azure-sql", "srv", "db", "u", "proj"


# --- resolution precedence ----------------------------------------------------------


def test_typed_password_beats_secret_ref():
    pw, ref = credentials.resolve_effective_password(
        _ST, _H, _D, _U, _P, "typed", SecretRef(scope="db-scope", key="sql-pw"))
    assert pw == "typed" and ref is None


def test_request_secret_ref_is_resolved():
    pw, ref = credentials.resolve_effective_password(
        _ST, _H, _D, _U, _P, "",
        SecretRef(kind="azure_key_vault", scope="kv-scope", key="vault-secret"))
    assert pw == "from-keyvault" and ref is not None and ref.scope == "kv-scope"


def test_secret_ref_is_bound_to_the_active_workspace(monkeypatch):
    calls: list[tuple[str, str]] = []

    class Secrets:
        def get_secret(self, *, scope, key):
            calls.append((scope, key))
            return SimpleNamespace(value=base64.b64encode(b"workspace-password").decode())

    client = SimpleNamespace(
        config=SimpleNamespace(host="https://adb-123.example.net/"),
        secrets=Secrets(),
    )
    monkeypatch.setattr("backend.config.workspace_client", lambda: client)

    matching = SecretRef(
        workspace_host="adb-123.example.net", scope="db-scope", key="sql-pw"
    )
    assert credentials._read_secret_value(matching) == "workspace-password"
    assert calls == [("db-scope", "sql-pw")]

    wrong = SecretRef(
        workspace_host="adb-999.example.net", scope="db-scope", key="sql-pw"
    )
    assert credentials._read_secret_value(wrong) is None
    # A mismatch is rejected before calling the Secrets API in the wrong workspace.
    assert calls == [("db-scope", "sql-pw")]


def test_stored_ref_used_when_request_is_empty():
    credentials.remember_secret_ref(_ST, _H, _D, _U, SecretRef(scope="db-scope", key="sql-pw"), _P)
    pw, ref = credentials.resolve_effective_password(_ST, _H, _D, _U, _P, "", None)
    assert pw == "from-databricks" and ref is not None and ref.key == "sql-pw"


def test_cached_plaintext_is_the_last_resort():
    credentials.remember_password(_ST, _H, _D, _U, "cached", _P)
    pw, ref = credentials.resolve_effective_password(_ST, _H, _D, _U, _P, "", None)
    assert pw == "cached" and ref is None


# --- persistence: pointer, not value ------------------------------------------------


def test_remember_effective_with_ref_stores_pointer_not_value():
    ref = SecretRef(
        workspace_host="adb-123.example.net", scope="db-scope", key="sql-pw"
    )
    credentials.remember_effective(_ST, _H, _D, _U, _P, "from-databricks", ref)
    # No plaintext is cached — only the reference, re-resolved live.
    assert credentials.resolve_password(_ST, _H, _D, _U, _P) is None
    stored = credentials.resolve_stored_secret_ref(_ST, _H, _D, _U, _P)
    assert stored is not None and (stored.scope, stored.key) == ("db-scope", "sql-pw")
    assert stored.workspace_host == "adb-123.example.net"


def test_switching_from_ref_to_plaintext_clears_the_ref():
    credentials.remember_effective(_ST, _H, _D, _U, _P, "x", SecretRef(scope="db-scope", key="sql-pw"))
    credentials.remember_effective(_ST, _H, _D, _U, _P, "plain", None)
    assert credentials.resolve_stored_secret_ref(_ST, _H, _D, _U, _P) is None
    assert credentials.resolve_password(_ST, _H, _D, _U, _P) == "plain"


# --- assessment route end to end ----------------------------------------------------


_REQ = {"source_type": "azure-sql", "host": "srv", "database": "db",
        "username": "u", "port": 1433}


@pytest.fixture
def client(monkeypatch):
    seen: dict = {}

    def fake_run_assessment(conn, *, use_ai=True, endpoint=None):
        seen["password"] = conn.password
        return AssessmentReport(
            database="db", table_count=0, total_rows=0, programmable_object_count=0,
            findings=[], readiness_score=100, severity_counts={}, tables=[],
            programmable_objects=[],
        )

    monkeypatch.setattr(assessment_routes, "run_assessment", fake_run_assessment)
    monkeypatch.setattr(
        "backend.connectors.azure_sql.AzureSqlConnection.test_connection", lambda self: True)
    app = FastAPI()
    app.include_router(assessment_routes.router)
    return TestClient(app), seen


def test_scan_resolves_password_from_secret_ref(client):
    c, seen = client
    body = {**_REQ, "password": "", "secret_ref": {"kind": "databricks", "scope": "db-scope", "key": "sql-pw"}}
    r = c.post("/api/assessment/scan", json=body)
    assert r.status_code == 200
    assert seen["password"] == "from-databricks"


def test_scan_persists_the_ref_so_a_reload_reresolves(client):
    c, seen = client
    # A successful scan with a secret_ref persists the pointer…
    r = c.post("/api/assessment/scan", json={
        **_REQ, "project_id": "P",
        "secret_ref": {"kind": "azure_key_vault", "scope": "kv-scope", "key": "vault-secret"}})
    assert r.status_code == 200 and seen["password"] == "from-keyvault"
    # …so a later request with neither password nor ref (page reload) still works,
    # and no plaintext was ever cached.
    assert credentials.resolve_password("azure-sql", "srv", "db", "u", "P") is None
    r = c.post("/api/assessment/scan", json={**_REQ, "password": "", "project_id": "P"})
    assert r.status_code == 200 and seen["password"] == "from-keyvault"
