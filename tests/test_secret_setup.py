"""Secret-scope upsert logic — exercised with a fake workspace client, no live workspace."""
from databricks.sdk.errors import ResourceAlreadyExists

from backend.migration import secret_setup


class _FakeSecrets:
    def __init__(self, scope_exists: bool):
        self.scope_exists = scope_exists
        self.created_scopes: list[str] = []
        self.put: dict[str, str] = {}

    def create_scope(self, scope: str):
        if self.scope_exists:
            raise ResourceAlreadyExists(f"Scope {scope} already exists!")
        self.created_scopes.append(scope)

    def put_secret(self, scope: str, key: str, string_value: str):
        self.put[key] = string_value


class _FakeClient:
    def __init__(self, scope_exists: bool):
        self.secrets = _FakeSecrets(scope_exists)


def _patch(monkeypatch, scope_exists: bool) -> _FakeClient:
    client = _FakeClient(scope_exists)
    monkeypatch.setattr(secret_setup, "workspace_client", lambda: client)
    return client


def test_creates_scope_and_writes_secrets(monkeypatch):
    client = _patch(monkeypatch, scope_exists=False)
    out = secret_setup.ensure_secret_scope("s", {"a": "1", "b": "2"})
    assert client.secrets.created_scopes == ["s"]
    assert out == {"scope": "s", "created": True, "keys": ["a", "b"]}


def test_existing_scope_is_reused_and_values_updated(monkeypatch):
    """The SDK raises ResourceAlreadyExists whose message has no RESOURCE_ALREADY_EXISTS
    literal — an existing scope must still be reused, not surfaced as an error."""
    client = _patch(monkeypatch, scope_exists=True)
    out = secret_setup.ensure_secret_scope("s", {"a": "1"})
    assert out == {"scope": "s", "created": False, "keys": ["a"]}
    assert client.secrets.put == {"a": "1"}


def test_empty_values_are_skipped(monkeypatch):
    client = _patch(monkeypatch, scope_exists=True)
    out = secret_setup.ensure_secret_scope("s", {"a": "", "b": "2"})
    assert out["keys"] == ["b"]
    assert client.secrets.put == {"b": "2"}
