"""Which stores are in use, and whether a restart keeps them (backend/storage_status.py).

Every store falls back rather than fail — projects to the container's filesystem,
credentials and run state to process memory — so a deployment that never received
LBX_PROJECTS_BACKEND=postgres looked exactly like an empty app and lost everything on
its next restart. Nothing said so, in the logs or the UI.
"""
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import storage_status as mod
from backend.api import settings_routes
from backend.connectors import credential_store as cred_mod
from backend.projects import store as projects_mod
from backend import run_store as runs_mod


# Named to match the real classes — _describe classifies by class name, and
# test_every_store_class_is_classified keeps that mapping honest.
class PostgresStore: ...
class PostgresCredentialStore: ...
class PostgresRunStore: ...
class LocalFileStore: ...
class MemoryCredentialStore: ...
class MemoryRunStore: ...


def _stores(monkeypatch, projects, credentials, runs) -> list[mod.StoreStatus]:
    monkeypatch.setattr(projects_mod, "get_store", projects)
    monkeypatch.setattr(cred_mod, "get_credential_store", credentials)
    monkeypatch.setattr(runs_mod, "get_run_store", runs)
    return mod.storage_status()


def _all_lakebase(monkeypatch) -> list[mod.StoreStatus]:
    return _stores(monkeypatch, PostgresStore, PostgresCredentialStore, PostgresRunStore)


def _nothing_durable(monkeypatch) -> list[mod.StoreStatus]:
    return _stores(monkeypatch, LocalFileStore, MemoryCredentialStore, MemoryRunStore)


def test_a_lakebase_backed_app_is_durable(monkeypatch):
    stores = _all_lakebase(monkeypatch)

    assert [s.backend for s in stores] == ["lakebase"] * 3
    assert all(s.durable for s in stores)
    assert mod.warning(stores) is None


def test_the_default_deployment_is_not_durable(monkeypatch):
    stores = _nothing_durable(monkeypatch)

    assert [s.backend for s in stores] == ["local-files", "memory", "memory"]
    assert not any(s.durable for s in stores)


def test_the_warning_names_only_what_would_be_lost(monkeypatch):
    stores = _stores(monkeypatch, PostgresStore, PostgresCredentialStore, MemoryRunStore)

    note = mod.warning(stores)

    assert note.split(" are not stored")[0] == "Runs"


def test_the_warning_says_how_to_fix_it(monkeypatch):
    note = mod.warning(_nothing_durable(monkeypatch))

    assert "LBX_PROJECTS_BACKEND=postgres" in note


def test_a_lakebase_store_reports_where_it_writes(monkeypatch):
    monkeypatch.setenv("LBX_PROJECTS_PG_HOST", "ep-x.database.azuredatabricks.net")
    monkeypatch.setenv("LBX_RUNS_PG_TABLE", "lbx_runs_v2")

    projects, _credentials, runs = _all_lakebase(monkeypatch)

    assert "ep-x.database.azuredatabricks.net" in projects.detail
    assert "lbx_projects" in projects.detail
    assert "lbx_runs_v2" in runs.detail


def _boom():
    raise RuntimeError("connection to server failed: no password supplied")


def test_an_unreachable_project_store_says_why(monkeypatch):
    """get_store is the one that raises instead of falling back — a missing secret or
    env var must read as a reason, not as an empty app."""
    projects, _c, _r = _stores(monkeypatch, _boom, MemoryCredentialStore, MemoryRunStore)

    assert projects.backend == "unavailable"
    assert projects.durable is False
    assert "no password supplied" in projects.detail


def test_an_unreachable_store_is_not_blamed_on_a_missing_env_var(monkeypatch):
    """It is configured for Lakebase already — repeating that advice hides the real
    error and sends you to the wrong place."""
    stores = _stores(monkeypatch, _boom, MemoryCredentialStore, MemoryRunStore)

    note = mod.warning(stores)

    assert "cannot be opened" in note
    assert "no password supplied" in note
    assert "LBX_PROJECTS_BACKEND" not in note


def test_an_unknown_store_class_is_never_assumed_durable(monkeypatch):
    class SomeFutureStore: ...

    projects, _c, _r = _stores(monkeypatch, SomeFutureStore, MemoryCredentialStore, MemoryRunStore)

    assert projects.durable is False
    assert projects.backend == "SomeFutureStore"


def test_every_store_class_is_classified():
    """A renamed or new backend would otherwise be silently reported as ephemeral."""
    from backend.connectors.credential_store import (
        MemoryCredentialStore as RealMemoryCred,
        PostgresCredentialStore as RealPgCred,
    )
    from backend.projects.store import (
        LocalFileStore as RealLocal,
        PostgresStore as RealPg,
        VolumeStore as RealVolume,
    )
    from backend.run_store import MemoryRunStore as RealMemoryRuns, PostgresRunStore as RealPgRuns

    for cls in (RealPg, RealVolume, RealLocal, RealPgCred, RealMemoryCred, RealPgRuns, RealMemoryRuns):
        assert cls.__name__ in mod._BACKENDS, f"{cls.__name__} is not classified"


def test_startup_logs_the_stores_and_warns_when_they_are_ephemeral(monkeypatch, caplog):
    _nothing_durable(monkeypatch)

    with caplog.at_level(logging.INFO, logger="lakebase_express.storage"):
        mod.log_storage()

    said = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any(level == logging.INFO and "projects=local-files" in msg for level, msg in said)
    assert any(level == logging.WARNING and "lost" in msg for level, msg in said)


def test_a_durable_app_logs_no_warning(monkeypatch, caplog):
    _all_lakebase(monkeypatch)

    with caplog.at_level(logging.INFO, logger="lakebase_express.storage"):
        mod.log_storage()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- The endpoint the UI reads ----------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(settings_routes.router)
    return TestClient(app)


def test_the_endpoint_reports_every_store(client, monkeypatch):
    _nothing_durable(monkeypatch)

    body = client.get("/api/settings/storage").json()

    assert [s["store"] for s in body["stores"]] == ["projects", "credentials", "runs"]
    assert body["durable"] is False
    assert "lost" in body["warning"]


def test_the_endpoint_is_quiet_when_everything_is_durable(client, monkeypatch):
    _all_lakebase(monkeypatch)

    body = client.get("/api/settings/storage").json()

    assert body["durable"] is True
    assert body["warning"] is None
