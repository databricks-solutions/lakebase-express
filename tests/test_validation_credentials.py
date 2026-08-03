"""Validation routes falling back to the session credential cache.

The Post-Migration Validation module connects to both sides like the migration
routes do, so it inherits the same contract (see test_target_session_credentials
and test_session_credentials): an empty password resolves from the last one that
authenticated this session, and a clear 400 names which password is missing.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import validation_routes
from backend.assessment.models import ConnectionRequest
from backend.connectors import credentials
from backend.migration.models import LakebaseConnRequest
from backend.validation import runs
from backend.validation.models import ValidationReport, ValidationRunRequest, ValidationRunState


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

_SOURCE = {
    "source_type": "azure-sql",
    "host": "srv.database.windows.net",
    "database": "xptoy",
    "username": "sqladmin@srv",
    "port": 1433,
}

_ITEM = {
    "id": "procedure:dbo.usp_Report",
    "kind": "procedure",
    "target_name": "public.usp_report",
    "status": "missing",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(validation_routes, "build_connector", lambda *a, **k: object())
    app = FastAPI()
    app.include_router(validation_routes.router)
    return TestClient(app)


def _prime(source: bool = True, target: bool = True):
    if source:
        credentials.remember_password(
            _SOURCE["source_type"], _SOURCE["host"], _SOURCE["database"],
            _SOURCE["username"], "srcpw",
        )
    if target:
        credentials.remember_password(
            credentials.LAKEBASE_NAMESPACE, _LB["host"], _LB["database"], _LB["user"], "pgpw",
        )


# --- /start ------------------------------------------------------------------


def test_start_resolves_both_sides_from_cache(client, monkeypatch):
    seen = {}

    def fake_start_run(req):
        seen["source"] = req.source.password
        seen["target"] = req.lakebase.password
        return "run-1"

    monkeypatch.setattr(validation_routes.runs, "start_run", fake_start_run)
    _prime()
    r = client.post("/api/validation/start", json={
        "source": {**_SOURCE, "password": ""},
        "lakebase": {**_LB, "password": ""},
    })
    assert r.status_code == 200 and r.json()["run_id"] == "run-1"
    assert seen == {"source": "srcpw", "target": "pgpw"}


def test_start_without_lakebase_password_or_cache_is_a_clear_400(client):
    _prime(target=False)
    r = client.post("/api/validation/start", json={
        "source": {**_SOURCE, "password": ""},
        "lakebase": {**_LB, "password": ""},
    })
    assert r.status_code == 400
    assert "lakebase password" in r.json()["detail"].lower()


# --- /repair/start -----------------------------------------------------------


def test_repair_start_resolves_password_from_cache(client, monkeypatch):
    seen = {}

    def fake_start_repair(req):
        seen["password"] = req.lakebase.password
        return "run-2"

    monkeypatch.setattr(validation_routes.agent, "start_repair", fake_start_repair)
    _prime(source=False)
    r = client.post("/api/validation/repair/start", json={
        "lakebase": {**_LB, "password": ""},
        "targets": [{"item": _ITEM}],
    })
    assert r.status_code == 200 and r.json()["run_id"] == "run-2"
    assert seen["password"] == "pgpw"


def test_repair_start_without_password_or_cache_is_a_clear_400(client):
    r = client.post("/api/validation/repair/start", json={
        "lakebase": {**_LB, "password": ""},
        "targets": [{"item": _ITEM}],
    })
    assert r.status_code == 400
    assert "re-enter" in r.json()["detail"].lower()


# --- Successful runs feed the cache ------------------------------------------


def test_successful_run_remembers_both_passwords(monkeypatch):
    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: SimpleNamespace(database="t"))
    monkeypatch.setattr(
        runs, "run_validation",
        lambda src, tgt, schema, progress, scope="full", use_estimates=True: ValidationReport(
            source_database="s", target_database="t", target_schema="public",
        ),
    )

    req = ValidationRunRequest(
        source=ConnectionRequest(**{**_SOURCE, "password": "srcpw"}),
        lakebase=LakebaseConnRequest(**{**_LB, "password": "pgpw"}),
    )
    run_id = "cred-run"
    with runs._LOCK:
        runs._RUNS[run_id] = ValidationRunState(run_id=run_id)
    runs._execute(run_id, req)

    assert runs.get_run(run_id).status == "success"
    assert credentials.resolve_password(
        credentials.LAKEBASE_NAMESPACE, _LB["host"], _LB["database"], _LB["user"]
    ) == "pgpw"
    assert credentials.resolve_password(
        _SOURCE["source_type"], _SOURCE["host"], _SOURCE["database"], _SOURCE["username"]
    ) == "srcpw"
