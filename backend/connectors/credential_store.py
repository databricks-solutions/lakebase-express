"""Credential persistence — pluggable backends, mirroring ``projects/store.py``.

Connection passwords were historically kept only in process memory (lost on any
App restart). This adds an optional durable backend so a resumed session — or a
redeployed App — can re-authenticate the source and Lakebase without the user
re-typing passwords, **without ever writing a password in clear text**.

Two backends behind one interface:
  * ``MemoryCredentialStore``    — the original in-process dict. Default; used in
    dev and unit tests (no Databricks/Lakebase needed).
  * ``PostgresCredentialStore``  — one row per connection in a Lakebase table,
    with the password Fernet-encrypted before it leaves the process. Only
    ciphertext is stored. Reuses the same Lakebase connection as the project
    store (``LBX_PROJECTS_PG_*``); selected automatically when that store is
    Postgres-backed.

The Fernet key lives in the Databricks secret scope (never in the database), so
a dump of the credentials table alone cannot decrypt anything. The key is
auto-generated on first use if the scope has none.
"""
from __future__ import annotations

import base64
import functools
import logging
import os
import threading
from abc import ABC, abstractmethod

log = logging.getLogger("lakebase_express.credential_store")

# Secret-scope key that holds the Fernet encryption key for stored credentials.
_CREDENTIAL_KEY_SECRET_KEY = os.getenv("LBX_CREDENTIAL_KEY_SECRET_KEY", "lbx-credential-key")


# (project_id, namespace, host, database, username). project_id scopes a
# credential to one migration project; "" is the legacy project-agnostic key.
CredKey = tuple[str, str, str, str, str]

# A pointer to a secret-manager entry: (kind, scope, key, workspace_host).
# Unlike a password this is NOT secret — it names where the password lives — so
# it is stored in clear text. workspace_host prevents a persisted scope/key from
# being resolved against a different active OAuth workspace.
RefValue = tuple[str, str, str, str | None]


class CredentialStore(ABC):
    @abstractmethod
    def remember(self, key: CredKey, password: str) -> None: ...
    @abstractmethod
    def resolve(self, key: CredKey) -> str | None: ...
    @abstractmethod
    def remember_ref(self, key: CredKey, ref: RefValue) -> None: ...
    @abstractmethod
    def resolve_ref(self, key: CredKey) -> RefValue | None: ...
    @abstractmethod
    def clear_project(self, project_id: str) -> None: ...
    @abstractmethod
    def clear(self) -> None: ...


class MemoryCredentialStore(CredentialStore):
    """In-process cache — the original behaviour. Nothing touches disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._passwords: dict[CredKey, str] = {}
        self._refs: dict[CredKey, RefValue] = {}

    def remember(self, key: CredKey, password: str) -> None:
        # A typed password supersedes any earlier secret-manager pointer.
        with self._lock:
            self._passwords[key] = password
            self._refs.pop(key, None)

    def resolve(self, key: CredKey) -> str | None:
        with self._lock:
            return self._passwords.get(key)

    def remember_ref(self, key: CredKey, ref: RefValue) -> None:
        # A secret-manager pointer supersedes any earlier typed password.
        with self._lock:
            self._refs[key] = ref
            self._passwords.pop(key, None)

    def resolve_ref(self, key: CredKey) -> RefValue | None:
        with self._lock:
            return self._refs.get(key)

    def clear_project(self, project_id: str) -> None:
        """Drop only credentials scoped to one migration project."""
        pid = project_id.strip()
        with self._lock:
            self._passwords = {
                key: value for key, value in self._passwords.items() if key[0] != pid
            }
            self._refs = {
                key: value for key, value in self._refs.items() if key[0] != pid
            }

    def clear(self) -> None:
        with self._lock:
            self._passwords.clear()
            self._refs.clear()


# --- Encryption key (Databricks secret scope, auto-generated) ----------------------


def _secret_scope() -> str:
    # Reuse the project store's scope override if set, else the app-wide scope.
    return os.getenv("LBX_PROJECTS_PG_SECRET_SCOPE") or os.getenv("LBX_SECRET_SCOPE", "lakebase-express")


@functools.lru_cache(maxsize=1)
def _fernet():
    """Return a ``Fernet`` bound to the key in the secret scope, creating one if
    absent. Cached for the process lifetime — the key never changes mid-run."""
    from cryptography.fernet import Fernet

    from backend.config import workspace_client

    w = workspace_client()
    scope, key = _secret_scope(), _CREDENTIAL_KEY_SECRET_KEY
    try:
        resp = w.secrets.get_secret(scope=scope, key=key)
        raw = base64.b64decode(resp.value).decode("utf-8") if resp.value else ""
    except Exception:
        raw = ""  # scope/key absent — generate below
    if not raw:
        raw = Fernet.generate_key().decode("utf-8")
        w.secrets.put_secret(scope=scope, key=key, string_value=raw)
        log.info("Generated a new credential-encryption key in scope %s (key %s)", scope, key)
    return Fernet(raw.encode("utf-8"))


class PostgresCredentialStore(CredentialStore):
    """Stores connection passwords in a Lakebase (Postgres) table, encrypted.

    One row per connection, keyed by the connection's identifying columns
    (``project_id, namespace, host, dbname, username``) with the ciphertext in
    ``secret`` — the plaintext password is never written. ``project_id`` is TEXT,
    not UUID: it carries the empty-string "project-agnostic" scope for bare
    callers, and a composite PRIMARY KEY can't hold a nullable UUID. The Lakebase
    connection is app-level, shared with the project store.
    """

    def __init__(self, *, host: str, database: str, user: str, port: int,
                 password: str, sslmode: str = "require", table: str = "lbx_credentials"):
        self._conn_kwargs = dict(
            host=host, dbname=database, user=user, password=password,
            port=port, sslmode=sslmode, connect_timeout=15,
            application_name="lakebase-express-credentials",
        )
        # Identifier is from config, not user input; keep it simple and quote it.
        self._table = '"' + table.replace('"', "") + '"'
        self._ensured = False

    def _connect(self):
        import psycopg

        conn = psycopg.connect(**self._conn_kwargs)
        if not self._ensured:
            with conn.cursor() as cur:
                # ``secret`` is nullable: a row may instead carry a secret-manager
                # pointer (ref_kind/ref_scope/ref_key) and no encrypted password.
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} ("
                    "project_id TEXT NOT NULL DEFAULT '', "
                    "namespace  TEXT NOT NULL, "
                    "host       TEXT NOT NULL, "
                    "dbname     TEXT NOT NULL, "
                    "username   TEXT NOT NULL, "
                    "secret     TEXT, "
                    "ref_kind   TEXT, "
                    "ref_scope  TEXT, "
                    "ref_key    TEXT, "
                    "ref_workspace_host TEXT, "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "PRIMARY KEY (project_id, namespace, host, dbname, username))"
                )
                # Bring a pre-existing table (secret NOT NULL, no ref columns) up to
                # the current shape without a separate migration step.
                cur.execute(f"ALTER TABLE {self._table} ALTER COLUMN secret DROP NOT NULL")
                for col in ("ref_kind", "ref_scope", "ref_key", "ref_workspace_host"):
                    cur.execute(f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS {col} TEXT")
            conn.commit()
            self._ensured = True
        return conn

    def remember(self, key: CredKey, password: str) -> None:
        token = _fernet().encrypt(password.encode("utf-8")).decode("utf-8")
        with self._connect() as conn, conn.cursor() as cur:
            # A typed password supersedes any earlier secret-manager pointer.
            cur.execute(
                f"INSERT INTO {self._table} "
                "(project_id, namespace, host, dbname, username, secret, "
                "ref_kind, ref_scope, ref_key, ref_workspace_host, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, now()) "
                "ON CONFLICT (project_id, namespace, host, dbname, username) "
                "DO UPDATE SET secret = EXCLUDED.secret, "
                "ref_kind = NULL, ref_scope = NULL, ref_key = NULL, "
                "ref_workspace_host = NULL, updated_at = now()",
                (*key, token),
            )
            conn.commit()

    def resolve(self, key: CredKey) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT secret FROM {self._table} WHERE "
                "project_id = %s AND namespace = %s AND host = %s AND dbname = %s AND username = %s",
                key,
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        try:
            return _fernet().decrypt(row[0].encode("utf-8")).decode("utf-8")
        except Exception as exc:  # key rotated, corrupt token — treat as a miss
            log.warning("Could not decrypt stored credential: %s", exc)
            return None

    def remember_ref(self, key: CredKey, ref: RefValue) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            # A secret-manager pointer supersedes any earlier typed password.
            cur.execute(
                f"INSERT INTO {self._table} "
                "(project_id, namespace, host, dbname, username, secret, "
                "ref_kind, ref_scope, ref_key, ref_workspace_host, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, now()) "
                "ON CONFLICT (project_id, namespace, host, dbname, username) "
                "DO UPDATE SET secret = NULL, ref_kind = EXCLUDED.ref_kind, "
                "ref_scope = EXCLUDED.ref_scope, ref_key = EXCLUDED.ref_key, "
                "ref_workspace_host = EXCLUDED.ref_workspace_host, updated_at = now()",
                (*key, *ref),
            )
            conn.commit()

    def resolve_ref(self, key: CredKey) -> RefValue | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT ref_kind, ref_scope, ref_key, ref_workspace_host FROM {self._table} WHERE "
                "project_id = %s AND namespace = %s AND host = %s AND dbname = %s AND username = %s",
                key,
            )
            row = cur.fetchone()
        if not row or not row[1] or not row[2]:  # need at least scope + key
            return None
        return (row[0] or "databricks", row[1], row[2], row[3])

    def clear_project(self, project_id: str) -> None:
        """Delete every source/target credential belonging to ``project_id``.

        Legacy project-agnostic rows (project_id = '') are intentionally left
        alone because they cannot be attributed to a deleted project.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._table} WHERE project_id = %s", (project_id.strip(),))
            conn.commit()

    def clear(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._table}")
            conn.commit()


@functools.lru_cache(maxsize=1)
def get_credential_store() -> CredentialStore:
    """Postgres-backed when the project store is (same Lakebase connection),
    otherwise the in-process memory cache."""
    backend = os.getenv("LBX_PROJECTS_BACKEND", "local").lower()
    if backend == "postgres":
        from backend.projects.store import _resolve_store_password

        try:
            return PostgresCredentialStore(
                host=os.environ["LBX_PROJECTS_PG_HOST"],
                database=os.getenv("LBX_PROJECTS_PG_DATABASE", "databricks_postgres"),
                user=os.environ["LBX_PROJECTS_PG_USER"],
                port=int(os.getenv("LBX_PROJECTS_PG_PORT", "5432")),
                password=_resolve_store_password(),
                table=os.getenv("LBX_CREDENTIALS_PG_TABLE", "lbx_credentials"),
            )
        except Exception as exc:
            # Never let credential persistence break the app — fall back to memory.
            log.warning("Postgres credential store unavailable, using in-memory cache: %s", exc)
    return MemoryCredentialStore()
