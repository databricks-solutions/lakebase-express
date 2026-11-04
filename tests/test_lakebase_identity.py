"""Lakebase target in Databricks identity mode: the signed-in user's own role over
an OAuth credential minted from their forwarded token — never a password."""
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import migration_routes
from backend.connectors import lakebase, lakebase_identity
from backend.migration.models import LakebaseConnRequest

_HOST = "ep-x.database.azuredatabricks.net"
_EP = "projects/mig/branches/production/endpoints/primary"
_LB = {"host": _HOST, "database": "xpmig", "user": "someone-else", "auth_mode": "databricks"}


class _FakeClient:
    """A WorkspaceClient as one user's forwarded token sees it."""

    minted: list[tuple[str, str]] = []

    def __init__(self, token: str):
        self.token = token
        self.config = SimpleNamespace(token=token)
        self.current_user = SimpleNamespace(me=lambda: SimpleNamespace(user_name="Ana@Corp.com"))
        ep = SimpleNamespace(name=_EP, status=SimpleNamespace(hosts=SimpleNamespace(host=_HOST)))
        self.postgres = SimpleNamespace(
            list_projects=lambda: [SimpleNamespace(name="projects/mig")],
            list_branches=lambda p: [SimpleNamespace(name=f"{p}/branches/production")],
            list_endpoints=lambda b: [ep],
            generate_database_credential=self._mint,
        )

    def _mint(self, endpoint):
        _FakeClient.minted.append((self.token, endpoint))
        return SimpleNamespace(token=f"pg-{self.token}")


@pytest.fixture(autouse=True)
def deployed(monkeypatch):
    lakebase_identity.clear()
    _FakeClient.minted = []
    monkeypatch.setenv("DATABRICKS_APP_NAME", "lakebase-express")
    monkeypatch.setattr(lakebase_identity, "_user_client", _FakeClient)
    yield
    lakebase_identity.clear()


def _signed_in(token="tok-1", email="ana@corp.com"):
    return lakebase_identity.bind_request(email, token)


def test_identity_mode_connects_as_the_signed_in_user_without_a_password():
    _signed_in()
    lb = LakebaseConnRequest(**_LB, password="typed", project_id="p1")
    out = migration_routes.with_lakebase_password(lb)
    # The role is the Databricks user_name, not whatever the client sent.
    assert out.user == "Ana@Corp.com"
    assert out.password == "" and out.secret_ref is None
    assert out.conn_kwargs()["auth_mode"] == "databricks"


def test_deployed_without_a_forwarded_token_is_a_clear_400():
    lakebase_identity.bind_request(None, None)
    with pytest.raises(migration_routes.HTTPException) as exc:
        migration_routes.with_lakebase_password(LakebaseConnRequest(**_LB))
    assert exc.value.status_code == 400
    assert "`postgres` user authorization scope" in exc.value.detail


def test_locally_the_profile_is_the_user(monkeypatch):
    monkeypatch.delenv("DATABRICKS_APP_NAME")
    import backend.config as config

    monkeypatch.setattr(config, "current_workspace", lambda: {"connected": True, "user": "dev@corp.com"})
    lakebase_identity.bind_request(None, None)
    assert lakebase_identity.current_identity() == "dev@corp.com"


def test_credential_is_minted_with_the_users_token_and_cached():
    _signed_in()
    user = lakebase_identity.current_identity()
    assert lakebase_identity.credential(user, _HOST) == "pg-tok-1"
    assert lakebase_identity.credential(user, _HOST) == "pg-tok-1"
    assert _FakeClient.minted == [("tok-1", _EP)]


def test_a_later_request_refreshes_the_token_a_running_load_mints_with(monkeypatch):
    _signed_in("tok-1")
    user = lakebase_identity.current_identity()
    lakebase_identity.credential(user, _HOST)
    # The tab keeps polling with a fresh token; the cached credential then expires.
    _signed_in("tok-2")
    monkeypatch.setattr(lakebase_identity, "_CREDENTIAL_TTL_S", -1)
    assert lakebase_identity.credential(user, _HOST) == "pg-tok-2"


def test_an_unknown_host_names_the_problem():
    _signed_in()
    user = lakebase_identity.current_identity()
    with pytest.raises(lakebase_identity.IdentityAuthError, match="No Lakebase endpoint"):
        lakebase_identity.credential(user, "ep-other.database.azuredatabricks.net")


def _jwt(claims: dict) -> str:
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.sig"


def test_a_sign_in_without_the_postgres_scope_says_how_to_fix_it(monkeypatch):
    token = _jwt({"scope": "iam.current-user:read iam.access-control:read"})
    _signed_in(token)
    user = lakebase_identity.current_identity()

    def denied():
        raise RuntimeError(
            "Provided OAuth token does not have required scopes: postgres [ReqId: x]. "
            "Config: host=https://adb, token=***"
        )

    client = _FakeClient(token)
    client.postgres.list_projects = denied
    monkeypatch.setattr(lakebase_identity, "_user_client", lambda t: client)
    with pytest.raises(lakebase_identity.IdentityAuthError) as exc:
        lakebase_identity.credential(user, _HOST)
    msg = str(exc.value)
    assert "doesn't include the `postgres` scope" in msg
    assert "(it has: iam.current-user:read iam.access-control:read)" in msg
    assert "Config:" not in msg


def test_connect_uses_the_minted_token(monkeypatch):
    _signed_in()
    seen = {}
    monkeypatch.setattr(lakebase.psycopg, "connect", lambda **kw: seen.update(kw) or "conn")
    conn = lakebase.LakebaseConnection(
        host=_HOST, database="xpmig", user=lakebase_identity.current_identity(),
        password="", auth_mode="databricks",
    )
    assert conn.connect() == "conn"
    assert seen["user"] == "Ana@Corp.com" and seen["password"] == "pg-tok-1"


def test_a_user_without_a_role_gets_the_snippet_to_create_it(monkeypatch):
    _signed_in()

    def reject(**_):
        raise psycopg.OperationalError('FATAL: password authentication failed for user "Ana@Corp.com"')

    monkeypatch.setattr(lakebase.psycopg, "connect", reject)
    conn = lakebase.LakebaseConnection(
        host=_HOST, database="xpmig", user=lakebase_identity.current_identity(),
        password="", auth_mode="databricks",
    )
    with pytest.raises(lakebase_identity.IdentityAuthError) as exc:
        conn.connect()
    msg = str(exc.value)
    assert "databricks_create_role('Ana@Corp.com', 'USER')" in msg
    assert "databricks psql --project mig" in msg


def test_password_mode_is_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(lakebase.psycopg, "connect", lambda **kw: seen.update(kw))
    lakebase.LakebaseConnection(host=_HOST, database="d", user="lbx_app", password="pw").connect()
    assert seen["password"] == "pw"
    assert _FakeClient.minted == []


def test_the_test_route_reports_who_it_connected_as(monkeypatch):
    class _Ok:
        def __init__(self, **kw):
            self.kw = kw

        def test_connection(self):
            return True

    monkeypatch.setattr(migration_routes, "LakebaseConnection", _Ok)
    from backend import main

    app = FastAPI()
    app.middleware("http")(main.bind_actor)
    app.include_router(migration_routes.router)
    r = TestClient(app).post(
        "/api/migration/lakebase/test", json=_LB,
        headers={"x-forwarded-email": "ana@corp.com", "x-forwarded-access-token": "tok-1"},
    )
    assert r.json() == {"ok": True, "message": "Connected to Lakebase as Ana@Corp.com."}
