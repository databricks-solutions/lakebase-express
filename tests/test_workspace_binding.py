"""The app is bound to exactly one workspace, resolved from the ambient identity.

There is no in-app login or workspace switching: locally the workspace comes from
the CLI profile the backend was started with, and when deployed as a Databricks
App from the App's injected identity.
"""
import functools

import pytest
from types import SimpleNamespace
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


# --- Lakebase endpoint resolution ------------------------------------------------
#
# Minting an OAuth database credential needs the endpoint *resource path*, which the
# hostname does not contain — so it is resolved from the workspace instead of being
# configured by hand.

A_HOST = "ep-aaa.database.eastus2.azuredatabricks.net"
B_HOST = "ep-bbb.database.eastus2.azuredatabricks.net"
A_PATH = "projects/a/branches/production/endpoints/primary"
B_PATH = "projects/b/branches/production/endpoints/primary"
B_REPLICA = "projects/b/branches/production/endpoints/replica"

# Two projects, one with a second endpoint — the shape that makes an exact host
# match matter.
TREE = [(A_PATH, A_HOST), (B_PATH, B_HOST), (B_REPLICA, "ep-ccc.database.x.net")]


def _postgres(monkeypatch, rows, raises=None):
    """Fake ``w.postgres`` over rows of (endpoint resource path, host)."""
    def uniq(values):
        return [SimpleNamespace(name=v) for v in dict.fromkeys(values)]

    api = SimpleNamespace(
        list_projects=lambda: uniq(p.split("/branches/")[0] for p, _ in rows),
        list_branches=lambda project: uniq(
            p.split("/endpoints/")[0] for p, _ in rows if p.startswith(project + "/")
        ),
        list_endpoints=lambda branch: [
            SimpleNamespace(name=p, status=SimpleNamespace(hosts=SimpleNamespace(host=h)))
            for p, h in rows if p.startswith(branch + "/")
        ],
    )
    if raises is not None:
        def boom():
            raise raises
        api.list_projects = boom
    # lru_cached like the real one — this module's fixture clears it on teardown.
    client = functools.lru_cache(maxsize=1)(lambda: SimpleNamespace(postgres=api))
    monkeypatch.setattr(config, "workspace_client", client)


def test_the_endpoint_is_found_by_its_host(monkeypatch, lakebase_endpoint):
    _postgres(monkeypatch, TREE)
    assert lakebase_endpoint(A_HOST) == A_PATH
    # Picks the endpoint serving that host, not merely the project's first.
    assert lakebase_endpoint(B_HOST) == B_PATH


def test_the_host_match_is_exact(monkeypatch, lakebase_endpoint):
    """A near miss would mint a credential for the wrong database."""
    _postgres(monkeypatch, TREE)
    assert lakebase_endpoint("ep-aaa.database.eastus2.azuredatabricks.NET") == A_PATH
    assert lakebase_endpoint("ep-aa.database.eastus2.azuredatabricks.net") == ""
    assert lakebase_endpoint("ep-aaa") == ""
    assert lakebase_endpoint("") == ""


def test_an_unlistable_workspace_resolves_to_nothing(monkeypatch, lakebase_endpoint):
    """Listing Lakebase projects is a workspace read the deployed service principal
    may not have — it costs run reporting, never the app."""
    _postgres(monkeypatch, TREE, raises=PermissionError("cannot list projects"))
    assert lakebase_endpoint(A_HOST) == ""
