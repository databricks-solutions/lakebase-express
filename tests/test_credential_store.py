"""Credential store backends: in-memory default and encrypted Lakebase store."""
import base64

import pytest
from cryptography.fernet import Fernet

from backend.connectors import credential_store as cs
from backend.connectors import credentials as credential_api
from backend.connectors.credential_store import (
    MemoryCredentialStore,
    PostgresCredentialStore,
)

# CredKey: (project_id, namespace, host, dbname, username).
_KEY = ("proj-1", "azure-sql", "srv.database.windows.net", "db", "u@srv")


# --- MemoryCredentialStore ---------------------------------------------------------


def test_memory_store_round_trip():
    store = MemoryCredentialStore()
    assert store.resolve(_KEY) is None
    store.remember(_KEY, "pw")
    assert store.resolve(_KEY) == "pw"
    store.clear()
    assert store.resolve(_KEY) is None


# --- PostgresCredentialStore (fake psycopg connection; no live DB) -----------------


class _FakeCursor:
    """Stand-in over a shared {key: row} dict for the store's SQL. Each row is a
    dict {"secret": token|None, "ref": (kind, scope, key, workspace)|None} — a connection
    carries at most one of the two (the other is nulled on write)."""

    def __init__(self, rows):
        self._rows = rows
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE") or s.startswith("ALTER TABLE"):
            self._result = []
        elif s.startswith("SELECT secret FROM"):
            row = self._rows.get(tuple(params))
            self._result = [(row["secret"],)] if row and row.get("secret") is not None else []
        elif s.startswith("SELECT ref_kind"):
            row = self._rows.get(tuple(params))
            ref = row.get("ref") if row else None
            self._result = [ref] if ref is not None else []
        elif s.startswith("INSERT INTO"):
            # remember: (*key, token); remember_ref adds kind/scope/key/workspace.
            if len(params) == 6:  # password path
                *key, token = params
                self._rows[tuple(key)] = {"secret": token, "ref": None}
            else:  # secret-ref path — 5 key columns + the four reference fields
                *key, kind, scope, refkey, workspace = params
                self._rows[tuple(key)] = {
                    "secret": None,
                    "ref": (kind, scope, refkey, workspace),
                }
            self._result = []
        elif s.startswith("DELETE FROM"):
            if params:
                project_id = params[0]
                for key in [key for key in self._rows if key[0] == project_id]:
                    self._rows.pop(key)
            else:
                self._rows.clear()
            self._result = []
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, row_factory=None):
        return _FakeCursor(self._rows)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def pg_store(monkeypatch):
    # Deterministic Fernet — no Databricks secret scope needed in the test.
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(cs, "_fernet", lambda: fernet)
    store = PostgresCredentialStore(host="h", database="databricks_postgres",
                                    user="u", port=5432, password="p")
    rows: dict = {}
    store._connect = lambda: _FakeConn(rows)  # type: ignore[method-assign]
    return store, rows, fernet


def test_postgres_store_round_trip(pg_store):
    store, _, _ = pg_store
    assert store.resolve(_KEY) is None
    store.remember(_KEY, "s3cret")
    assert store.resolve(_KEY) == "s3cret"


def test_postgres_store_never_stores_plaintext(pg_store):
    store, rows, fernet = pg_store
    store.remember(_KEY, "s3cret")
    stored = next(iter(rows.values()))["secret"]
    # The stored value is ciphertext, not the password.
    assert "s3cret" not in stored
    assert fernet.decrypt(stored.encode()).decode() == "s3cret"


def test_postgres_store_upserts(pg_store):
    store, rows, _ = pg_store
    store.remember(_KEY, "old")
    store.remember(_KEY, "new")
    assert len(rows) == 1
    assert store.resolve(_KEY) == "new"


def test_postgres_store_undecryptable_token_is_a_miss(pg_store):
    store, rows, _ = pg_store
    # A token encrypted under a different key (e.g. rotated) can't be read back.
    rows[_KEY] = {"secret": Fernet(Fernet.generate_key()).encrypt(b"x").decode(), "ref": None}
    assert store.resolve(_KEY) is None


def test_postgres_store_clear(pg_store):
    store, _, _ = pg_store
    store.remember(_KEY, "s3cret")
    store.clear()
    assert store.resolve(_KEY) is None


def test_memory_store_clear_project_is_scoped():
    store = MemoryCredentialStore()
    other = ("proj-2", *_KEY[1:])
    legacy = ("", *_KEY[1:])
    store.remember(_KEY, "project-one")
    store.remember(other, "project-two")
    store.remember(legacy, "legacy")

    store.clear_project("proj-1")

    assert store.resolve(_KEY) is None
    assert store.resolve(other) == "project-two"
    assert store.resolve(legacy) == "legacy"


def test_postgres_store_clear_project_is_scoped(pg_store):
    store, _, _ = pg_store
    other = ("proj-2", *_KEY[1:])
    legacy = ("", *_KEY[1:])
    store.remember(_KEY, "project-one")
    store.remember(other, "project-two")
    store.remember(legacy, "legacy")

    store.clear_project("proj-1")

    assert store.resolve(_KEY) is None
    assert store.resolve(other) == "project-two"
    assert store.resolve(legacy) == "legacy"


# --- secret-manager references -----------------------------------------------------


def test_memory_store_ref_round_trip():
    store = MemoryCredentialStore()
    assert store.resolve_ref(_KEY) is None
    ref = ("azure_key_vault", "kv-scope", "sql-pw", "adb.example.net")
    store.remember_ref(_KEY, ref)
    assert store.resolve_ref(_KEY) == ref


def test_memory_store_ref_and_password_are_mutually_exclusive():
    store = MemoryCredentialStore()
    store.remember(_KEY, "pw")
    store.remember_ref(_KEY, ("databricks", "sc", "k", "adb.example.net"))
    # Storing a ref drops any earlier plaintext…
    assert store.resolve(_KEY) is None
    assert store.resolve_ref(_KEY) == ("databricks", "sc", "k", "adb.example.net")
    # …and storing a plaintext drops the ref.
    store.remember(_KEY, "pw2")
    assert store.resolve_ref(_KEY) is None
    assert store.resolve(_KEY) == "pw2"


def test_memory_store_clear_project_removes_secret_refs():
    store = MemoryCredentialStore()
    other = ("proj-2", *_KEY[1:])
    ref = ("databricks", "sc", "k", "adb.example.net")
    store.remember_ref(_KEY, ref)
    store.remember_ref(other, ref)

    store.clear_project("proj-1")

    assert store.resolve_ref(_KEY) is None
    assert store.resolve_ref(other) == ref


def test_postgres_store_ref_round_trip(pg_store):
    store, _, _ = pg_store
    assert store.resolve_ref(_KEY) is None
    store.remember_ref(_KEY, ("databricks", "sc", "k", "adb.example.net"))
    assert store.resolve_ref(_KEY) == ("databricks", "sc", "k", "adb.example.net")
    # A ref-only row has no password.
    assert store.resolve(_KEY) is None


def test_postgres_store_ref_never_stores_the_value(pg_store):
    store, rows, _ = pg_store
    store.remember_ref(_KEY, ("azure_key_vault", "kv", "name", "adb.example.net"))
    row = next(iter(rows.values()))
    # Only the pointer is stored; the secret column stays empty.
    assert row["secret"] is None
    assert row["ref"] == ("azure_key_vault", "kv", "name", "adb.example.net")


def test_postgres_store_password_then_ref_supersedes(pg_store):
    store, rows, _ = pg_store
    store.remember(_KEY, "pw")
    store.remember_ref(_KEY, ("databricks", "sc", "k", "adb.example.net"))
    assert len(rows) == 1
    assert store.resolve(_KEY) is None
    assert store.resolve_ref(_KEY) == ("databricks", "sc", "k", "adb.example.net")


# --- backend selection -------------------------------------------------------------


def test_get_credential_store_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("LBX_PROJECTS_BACKEND", raising=False)
    cs.get_credential_store.cache_clear()
    assert isinstance(cs.get_credential_store(), MemoryCredentialStore)
    cs.get_credential_store.cache_clear()


def test_get_credential_store_postgres_falls_back_to_memory_on_error(monkeypatch):
    # Postgres selected but the required host env is missing → safe memory fallback.
    monkeypatch.setenv("LBX_PROJECTS_BACKEND", "postgres")
    monkeypatch.delenv("LBX_PROJECTS_PG_HOST", raising=False)
    cs.get_credential_store.cache_clear()
    assert isinstance(cs.get_credential_store(), MemoryCredentialStore)
    cs.get_credential_store.cache_clear()


def test_project_cleanup_rejects_postgres_memory_fallback(monkeypatch):
    """A durable-store outage must retain the project instead of pretending its
    database rows were cleaned through the in-memory fallback."""
    monkeypatch.setenv("LBX_PROJECTS_BACKEND", "postgres")
    monkeypatch.setattr(credential_api, "get_credential_store", MemoryCredentialStore)

    with pytest.raises(RuntimeError, match="Postgres credential store is unavailable"):
        credential_api.clear_project("proj-1")
