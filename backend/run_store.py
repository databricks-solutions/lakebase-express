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
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("lakebase_express.run_store")


@dataclass(frozen=True)
class RunRecord:
    """One row of run history, without the state payload."""

    kind: str
    run_id: str
    status: str
    updated_at: str


class RunStore(ABC):
    @abstractmethod
    def save(self, kind: str, run_id: str, status: str, data: dict) -> None: ...
    @abstractmethod
    def load(self, kind: str, run_id: str) -> dict | None: ...
    @abstractmethod
    def list(self, kind: str | None = None, limit: int = 50) -> list[RunRecord]: ...


class MemoryRunStore(RunStore):
    """Process memory. Runs are lost on restart and invisible to other workers."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[str, dict, str]] = {}
        self._lock = threading.Lock()

    def save(self, kind: str, run_id: str, status: str, data: dict) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._rows[(kind, run_id)] = (status, data, stamp)

    def load(self, kind: str, run_id: str) -> dict | None:
        with self._lock:
            row = self._rows.get((kind, run_id))
            return row[1] if row else None

    def list(self, kind: str | None = None, limit: int = 50) -> list[RunRecord]:
        with self._lock:
            records = [
                RunRecord(kind=k, run_id=rid, status=status, updated_at=stamp)
                for (k, rid), (status, _data, stamp) in self._rows.items()
                if kind is None or k == kind
            ]
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records[:limit]


class PostgresRunStore(RunStore):
    """One row per run in a Lakebase table, keyed ``(kind, run_id)``.

    ``run_id`` is a short hex string, not a UUID, so it is stored as TEXT.
    """

    def __init__(self, *, host: str, database: str, user: str, port: int,
                 password: str, sslmode: str = "require", table: str = "lbx_runs"):
        self._conn_kwargs = dict(
            host=host, dbname=database, user=user, password=password,
            port=port, sslmode=sslmode, connect_timeout=15,
            application_name="lakebase-express-runs",
        )
        # Identifier is from config, not user input; keep it simple and quote it.
        self._table_raw = table.replace('"', "")
        self._table = f'"{self._table_raw}"'
        self._ensured = False

    def _connect(self):
        import psycopg

        conn = psycopg.connect(**self._conn_kwargs)
        if not self._ensured:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} ("
                    "kind TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL, "
                    "data JSONB NOT NULL, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "PRIMARY KEY (kind, run_id))"
                )
                # Unlike projects, this table grows one row per run.
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS "{self._table_raw}_updated_at_idx" '
                    f"ON {self._table} (updated_at DESC)"
                )
            conn.commit()
            self._ensured = True
        return conn

    def save(self, kind: str, run_id: str, status: str, data: dict) -> None:
        payload = json.dumps(data)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table} (kind, run_id, status, data, updated_at) "
                "VALUES (%s, %s, %s, %s::jsonb, now()) "
                "ON CONFLICT (kind, run_id) DO UPDATE SET "
                "status = EXCLUDED.status, data = EXCLUDED.data, updated_at = now()",
                (kind, run_id, status, payload),
            )
            conn.commit()

    def load(self, kind: str, run_id: str) -> dict | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT data FROM {self._table} WHERE kind = %s AND run_id = %s",
                (kind, run_id),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def list(self, kind: str | None = None, limit: int = 50) -> list[RunRecord]:
        from psycopg.rows import dict_row

        sql = f"SELECT kind, run_id, status, updated_at FROM {self._table}"
        params: list = []
        if kind is not None:
            sql += " WHERE kind = %s"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [
                RunRecord(kind=r["kind"], run_id=r["run_id"], status=r["status"],
                          updated_at=str(r["updated_at"]))
                for r in cur.fetchall()
            ]


@functools.lru_cache(maxsize=1)
def get_run_store() -> RunStore:
    """Postgres-backed when the project store is (same Lakebase connection),
    otherwise process memory. ``LBX_RUNS_BACKEND`` overrides."""
    default = "postgres" if os.getenv("LBX_PROJECTS_BACKEND", "local").lower() == "postgres" else "memory"
    if os.getenv("LBX_RUNS_BACKEND", default).lower() == "postgres":
        from backend.projects.store import _resolve_store_password

        try:
            return PostgresRunStore(
                host=os.environ["LBX_PROJECTS_PG_HOST"],
                database=os.getenv("LBX_PROJECTS_PG_DATABASE", "databricks_postgres"),
                user=os.environ["LBX_PROJECTS_PG_USER"],
                port=int(os.getenv("LBX_PROJECTS_PG_PORT", "5432")),
                password=_resolve_store_password(),
                table=os.getenv("LBX_RUNS_PG_TABLE", "lbx_runs"),
            )
        except Exception as exc:
            # Never let run persistence break the app — fall back to memory.
            log.warning("Postgres run store unavailable, using process memory: %s", exc)
    return MemoryRunStore()
