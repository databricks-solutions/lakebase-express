"""Run-state persistence — pluggable backends.

``MemoryRunStore`` (process memory, the default) or ``PostgresRunStore`` (one
JSONB row per run in a Lakebase table). Postgres-backed when the project store
is, reusing the same connection — see ``get_run_store``.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("lakebase_express.run_store")

# Derived, not random, so every task of a job run lands on one row. Mirrored by
# the generated notebooks (etl_generator._RUN_STATE).
RUN_ID_PREFIX = "lakebase-express/run/"


def run_state_id(job_id, job_run_id) -> str:
    """The run id a Databricks job run records itself under."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{RUN_ID_PREFIX}{job_id}:{job_run_id}"))


@dataclass(frozen=True)
class RunRecord:
    """One row of run history, without the state payload."""

    kind: str
    run_id: str
    status: str
    updated_at: str
    project_id: str | None = None


class RunStore(ABC):
    @abstractmethod
    def save(self, kind: str, run_id: str, status: str, data: dict,
             project_id: str | None = None) -> None: ...
    @abstractmethod
    def load(self, kind: str, run_id: str) -> dict | None: ...
    @abstractmethod
    def list(self, kind: str | None = None, limit: int = 50,
             project_id: str | None = None) -> list[RunRecord]: ...

    def notebook_config(self) -> dict | None:
        """Coordinates a Databricks job needs to record its own run state, or None
        when the store is unreachable from a job."""
        return None


class MemoryRunStore(RunStore):
    """Process memory. Runs are lost on restart and invisible to other workers."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[str, dict, str, str | None]] = {}
        self._lock = threading.Lock()

    def save(self, kind: str, run_id: str, status: str, data: dict,
             project_id: str | None = None) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            row = self._rows.get((kind, run_id))
            # project_id is set once, at insert.
            pid = row[3] if row else (project_id or None)
            self._rows[(kind, run_id)] = (status, data, stamp, pid)

    def load(self, kind: str, run_id: str) -> dict | None:
        with self._lock:
            row = self._rows.get((kind, run_id))
            return row[1] if row else None

    def list(self, kind: str | None = None, limit: int = 50,
             project_id: str | None = None) -> list[RunRecord]:
        with self._lock:
            records = [
                RunRecord(kind=k, run_id=rid, status=status, updated_at=stamp, project_id=pid)
                for (k, rid), (status, _data, stamp, pid) in self._rows.items()
                if (kind is None or k == kind) and (project_id is None or pid == project_id)
            ]
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records[:limit]


class PostgresRunStore(RunStore):
    """One row per run in a Lakebase table, keyed by a native ``uuid``.

    ``project_id`` is nullable and has no foreign key: the projects table may live
    in another backend entirely, and a missing reference must not stop a run being
    recorded.
    """

    def __init__(self, *, host: str, database: str, user: str, port: int,
                 password: str, sslmode: str = "require", table: str = "lbx_runs",
                 endpoint: str = ""):
        # projects/../branches/../endpoints/.. — only jobs use it, to mint OAuth.
        self._endpoint = endpoint
        self._conn_kwargs = dict(
            host=host, dbname=database, user=user, password=password,
            port=port, sslmode=sslmode, connect_timeout=15,
            application_name="lakebase-express-runs",
        )
        # From config, not user input — quoting is enough.
        self._table_raw = table.replace('"', "")
        self._table = f'"{self._table_raw}"'
        self._ensured = False

    def notebook_config(self) -> dict | None:
        """Non-secret coordinates for a job to write run state over OAuth. None
        without an endpoint path — there is no other way to authenticate."""
        if not self._endpoint:
            return None
        return {
            "host": self._conn_kwargs["host"],
            "port": self._conn_kwargs["port"],
            "database": self._conn_kwargs["dbname"],
            "table": self._table_raw,
            "endpoint": self._endpoint,
        }

    def grant_writer(self, identity: str) -> None:
        """Let a Databricks identity record runs — least privilege, idempotent.

        The identity needs a Lakebase role already; the app cannot create one from a
        password session, so a missing role raises undefined_object and the caller
        turns that into instructions.
        """
        role = '"' + identity.replace('"', '""') + '"'
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            cur.execute(f"GRANT SELECT, INSERT, UPDATE ON {self._table} TO {role}")
            conn.commit()

    @staticmethod
    def _as_uuid(value: str | None) -> str | None:
        """Validate before a ``::uuid`` cast, so a malformed id reads as 'no such
        run' instead of raising. Mirrors PostgresStore._as_uuid."""
        try:
            return str(uuid.UUID(value))  # type: ignore[arg-type]
        except (ValueError, AttributeError, TypeError):
            return None

    def _connect(self):
        import psycopg

        conn = psycopg.connect(**self._conn_kwargs)
        if not self._ensured:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} ("
                    "run_id UUID PRIMARY KEY, kind TEXT NOT NULL, "
                    "project_id UUID, status TEXT NOT NULL, data JSONB NOT NULL, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                # This table grows per run: history is read newest-first per kind.
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS "{self._table_raw}_kind_updated_idx" '
                    f"ON {self._table} (kind, updated_at DESC)"
                )
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS "{self._table_raw}_project_idx" '
                    f"ON {self._table} (project_id)"
                )
            conn.commit()
            self._ensured = True
        return conn

    def save(self, kind: str, run_id: str, status: str, data: dict,
             project_id: str | None = None) -> None:
        rid = self._as_uuid(run_id)
        if rid is None:
            raise ValueError(f"Run id is not a valid UUID: {run_id!r}")
        payload = json.dumps(data)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table} "
                "(run_id, kind, project_id, status, data, updated_at) "
                "VALUES (%s::uuid, %s, %s::uuid, %s, %s::jsonb, now()) "
                # project_id is immutable, so the upsert leaves it alone.
                "ON CONFLICT (run_id) DO UPDATE SET "
                "status = EXCLUDED.status, data = EXCLUDED.data, updated_at = now()",
                (rid, kind, self._as_uuid(project_id), status, payload),
            )
            conn.commit()

    def load(self, kind: str, run_id: str) -> dict | None:
        rid = self._as_uuid(run_id)
        if rid is None:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT data FROM {self._table} WHERE run_id = %s::uuid AND kind = %s",
                (rid, kind),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def list(self, kind: str | None = None, limit: int = 50,
             project_id: str | None = None) -> list[RunRecord]:
        from psycopg.rows import dict_row

        sql = f"SELECT run_id, kind, project_id, status, updated_at FROM {self._table}"
        where: list[str] = []
        params: list = []
        if kind is not None:
            where.append("kind = %s")
            params.append(kind)
        if project_id is not None:
            where.append("project_id = %s::uuid")
            params.append(self._as_uuid(project_id))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [
                RunRecord(kind=r["kind"], run_id=str(r["run_id"]), status=r["status"],
                          updated_at=str(r["updated_at"]),
                          project_id=str(r["project_id"]) if r["project_id"] else None)
                for r in cur.fetchall()
            ]


def _runs_endpoint(host: str) -> str:
    """Endpoint path the generated jobs authenticate against, resolved from the host
    so it needs no configuring. ``LBX_PROJECTS_PG_ENDPOINT`` overrides."""
    configured = os.getenv("LBX_PROJECTS_PG_ENDPOINT", "").strip()
    if configured:
        return configured
    from backend.config import lakebase_endpoint

    return lakebase_endpoint(host)


@functools.lru_cache(maxsize=1)
def get_run_store() -> RunStore:
    """Postgres-backed when the project store is (same Lakebase connection),
    otherwise process memory. ``LBX_RUNS_BACKEND`` overrides."""
    default = "postgres" if os.getenv("LBX_PROJECTS_BACKEND", "local").lower() == "postgres" else "memory"
    if os.getenv("LBX_RUNS_BACKEND", default).lower() == "postgres":
        from backend.projects.store import _resolve_store_password

        try:
            host = os.environ["LBX_PROJECTS_PG_HOST"]
            return PostgresRunStore(
                host=host,
                database=os.getenv("LBX_PROJECTS_PG_DATABASE", "databricks_postgres"),
                user=os.environ["LBX_PROJECTS_PG_USER"],
                port=int(os.getenv("LBX_PROJECTS_PG_PORT", "5432")),
                password=_resolve_store_password(),
                table=os.getenv("LBX_RUNS_PG_TABLE", "lbx_runs"),
                endpoint=_runs_endpoint(host),
            )
        except Exception as exc:
            # Never let run persistence break the app — fall back to memory.
            log.warning("Postgres run store unavailable, using process memory: %s", exc)
    return MemoryRunStore()
