"""The app is bound to exactly one workspace, resolved from the ambient identity.

There is no in-app login or workspace switching: locally the workspace comes from
the CLI profile the backend was started with, and when deployed as a Databricks
App from the App's injected identity.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config
from backend.api import databricks_routes


@pytest.fixture(autouse=True)
def _clear_caches():
    """current_workspace/workspace_client are lru_cached for the process lifetime."""
    config.current_workspace.cache_clear()
    config.workspace_client.cache_clear()
    yield
    config.current_workspace.cache_clear()
    config.workspace_client.cache_clear()


class _FakeWorkspaceClient:
    calls = 0

    def __init__(self, host="https://example.cloud.databricks.com", user="me@example.com"):
        type(self).calls += 1

        class _Config:
            pass

        self.config = _Config()
        self.config.host = host
        self._user = user

    @property
    def current_user(self):
        return self

    def me(self):
        class _Me:
            user_name = self._user

        return _Me()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(databricks_routes.router)
    return TestClient(app)


def test_status_reports_the_bound_workspace(monkeypatch):
    monkeypatch.setattr(config, "WorkspaceClient", _FakeWorkspaceClient)

    res = _client().get("/api/databricks/status")

    assert res.json() == {
        "connected": True,
        "host": "example.cloud.databricks.com",
        "user": "me@example.com",
    }


def test_status_strips_scheme_from_host(monkeypatch):
    monkeypatch.setattr(config, "WorkspaceClient",
                        lambda: _FakeWorkspaceClient(host="https://adb-123.11.azuredatabricks.net/"))

    assert _client().get("/api/databricks/status").json()["host"] == "adb-123.11.azuredatabricks.net"


def test_status_reports_missing_identity_without_raising(monkeypatch):
    """A misconfigured profile must not 500 — the UI needs the reason to show."""

    def _boom():
        raise ValueError("default auth: cannot configure default credentials")

    monkeypatch.setattr(config, "WorkspaceClient", _boom)

    res = _client().get("/api/databricks/status")

    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is False
    assert "default credentials" in body["error"]


def test_workspace_is_resolved_once(monkeypatch):
    """The target can't change mid-process, so the client is built a single time."""
    monkeypatch.setattr(config, "WorkspaceClient", _FakeWorkspaceClient)
    _FakeWorkspaceClient.calls = 0

    client = _client()
    for _ in range(3):
        client.get("/api/databricks/status")

    assert _FakeWorkspaceClient.calls == 1


def test_no_login_or_workspace_switching_endpoints():
    """Guards against reintroducing an in-app workspace switch."""
    paths = {route.path for route in databricks_routes.router.routes}

    assert not any("oauth" in p or "login" in p or "logout" in p for p in paths), paths


def test_config_exposes_no_session_mutators():
    """Nothing should be able to repoint the app at another workspace at runtime."""
    for gone in ("set_workspace_session", "clear_workspace_session", "has_oauth_session",
                 "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_SCOPES"):
        assert not hasattr(config, gone), f"{gone} should no longer exist"
