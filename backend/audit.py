"""Audit trail: who ran which migration step, and who touched which credential.

Append-only, pluggable like the run store: ``MemoryAuditStore`` (process memory,
the default) or ``PostgresAuditStore`` (one row per event in a Lakebase table, on
the project store's connection). Events never carry a password or a secret value —
only which connection, which secret-manager pointer, and how it was obtained.

Rows are never deleted by the app, not even with their project: the trail has to
outlive what it describes. Every event is also logged on ``lakebase_express.audit``,
so it reaches the app logs even when the store is unreachable.
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("lakebase_express.audit")

# Set per request from the Databricks Apps identity headers (see backend/main.py).
_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("lbx_actor", default=None)

# The Apps proxy sets these from the signed-in user; the first present wins.
IDENTITY_HEADERS = ("x-forwarded-email", "x-forwarded-preferred-username", "x-forwarded-user")

_MEMORY_LIMIT = 5000


@dataclass(frozen=True)
class AuditEvent:
    action: str                 # e.g. run.started, credential.read
    actor: str
    at: str = ""
    project_id: str | None = None
    target: str = ""            # what was acted on: a run id, a host/database, a scope
    outcome: str = "ok"         # ok | failed | a run's terminal status
    detail: dict = field(default_factory=dict)


def set_actor(actor: str | None) -> contextvars.Token:
    return _actor.set(actor)


def reset_actor(token: contextvars.Token) -> None:
    _actor.reset(token)


@functools.lru_cache(maxsize=1)
def _app_identity() -> str:
    from backend.config import current_workspace

    return current_workspace().get("user") or "unknown"


def current_actor() -> str:
    """The signed-in user, or the app's own identity marked as such.

    Locally there is no proxy, so the app identity is the developer's profile; in a
    deployed app it means the action did not come from a user request.
    """
    return _actor.get() or f"{_app_identity()} (app identity)"


def spawn(target: Callable, *args) -> None:
    """Start a daemon thread that keeps the caller's actor, so events a background
    run records are attributed to whoever started it."""
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(target, *args), daemon=True).start()


class AuditStore(ABC):
    @abstractmethod
    def append(self, event: AuditEvent) -> None: ...
    @abstractmethod
    def list(self, project_id: str | None = None, action: str | None = None,
             limit: int = 200) -> list[AuditEvent]:
        """Newest first. ``action`` matches exactly or as a prefix ending in '.'."""


def _matches(event: AuditEvent, project_id: str | None, action: str | None) -> bool:
    if project_id is not None and event.project_id != project_id:
        return False
    if action and not (event.action == action or (action.endswith(".") and event.action.startswith(action))):
        return False
    return True


class MemoryAuditStore(AuditStore):
    """Process memory, bounded. Lost on restart — only for dev and tests."""

    def __init__(self, limit: int = _MEMORY_LIMIT) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list(self, project_id: str | None = None, action: str | None = None,
             limit: int = 200) -> list[AuditEvent]:
        with self._lock:
            events = [e for e in reversed(self._events) if _matches(e, project_id, action)]
        return events[:limit]


class PostgresAuditStore(AuditStore):
    """One row per event in a Lakebase table.

    A trigger rejects UPDATE, DELETE and TRUNCATE, so a bug in the app cannot
    rewrite history; the table owner can still drop it for a retention purge.
    """

    def __init__(self, *, host: str, database: str, user: str, port: int,
                 password: str, sslmode: str = "require", table: str = "lbx_audit"):
        self._conn_kwargs = dict(
            host=host, dbname=database, user=user, password=password,
            port=port, sslmode=sslmode, connect_timeout=15,
            application_name="lakebase-express-audit",
        )
        # From config, not user input — quoting is enough.
        self._table_raw = table.replace('"', "")
        self._table = f'"{self._table_raw}"'
        self._ensured = False

    def _connect(self):
        import psycopg

        conn = psycopg.connect(**self._conn_kwargs)
        if not self._ensured:
            guard = f'"{self._table_raw}_append_only"'
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} ("
                    "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                    "at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                    "actor TEXT NOT NULL, action TEXT NOT NULL, project_id TEXT, "
                    "target TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, "
                    "detail JSONB NOT NULL DEFAULT '{}'::jsonb)"
                )
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS "{self._table_raw}_at_idx" ON {self._table} (at DESC)'
                )
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS "{self._table_raw}_project_idx" '
                    f"ON {self._table} (project_id, at DESC)"
                )
                cur.execute(
                    f"CREATE OR REPLACE FUNCTION {guard}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'the audit trail is append-only'; END; $$"
                )
                cur.execute(
                    f"CREATE OR REPLACE TRIGGER {guard} BEFORE UPDATE OR DELETE ON {self._table} "
                    f"FOR EACH ROW EXECUTE FUNCTION {guard}()"
                )
                cur.execute(
                    f'CREATE OR REPLACE TRIGGER "{self._table_raw}_no_truncate" '
                    f"BEFORE TRUNCATE ON {self._table} FOR EACH STATEMENT EXECUTE FUNCTION {guard}()"
                )
            conn.commit()
            self._ensured = True
        return conn

    def append(self, event: AuditEvent) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table} "
                "(at, actor, action, project_id, target, outcome, detail) "
                "VALUES (%s::timestamptz, %s, %s, %s, %s, %s, %s::jsonb)",
                (event.at, event.actor, event.action, event.project_id, event.target,
                 event.outcome, json.dumps(event.detail)),
            )
            conn.commit()

    def list(self, project_id: str | None = None, action: str | None = None,
             limit: int = 200) -> list[AuditEvent]:
        from psycopg.rows import dict_row

        sql = f"SELECT at, actor, action, project_id, target, outcome, detail FROM {self._table}"
        where: list[str] = []
        params: list = []
        if project_id is not None:
            where.append("project_id = %s")
            params.append(project_id)
        if action:
            if action.endswith("."):
                where.append("starts_with(action, %s)")
            else:
                where.append("action = %s")
            params.append(action)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY at DESC, id DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [
                AuditEvent(at=r["at"].isoformat(), actor=r["actor"], action=r["action"],
                           project_id=r["project_id"], target=r["target"],
                           outcome=r["outcome"], detail=r["detail"] or {})
                for r in cur.fetchall()
            ]


@functools.lru_cache(maxsize=1)
def get_audit_store() -> AuditStore:
    """Postgres-backed when the project store is (same Lakebase connection),
    otherwise process memory. ``LBX_AUDIT_BACKEND`` overrides."""
    default = "postgres" if os.getenv("LBX_PROJECTS_BACKEND", "local").lower() == "postgres" else "memory"
    if os.getenv("LBX_AUDIT_BACKEND", default).lower() == "postgres":
        from backend.projects.store import _resolve_store_password

        try:
            return PostgresAuditStore(
                host=os.environ["LBX_PROJECTS_PG_HOST"],
                database=os.getenv("LBX_PROJECTS_PG_DATABASE", "databricks_postgres"),
                user=os.environ["LBX_PROJECTS_PG_USER"],
                port=int(os.getenv("LBX_PROJECTS_PG_PORT", "5432")),
                password=_resolve_store_password(),
                table=os.getenv("LBX_AUDIT_PG_TABLE", "lbx_audit"),
            )
        except Exception as exc:
            log.warning("Postgres audit store unavailable, using process memory: %s", exc)
    return MemoryAuditStore()


def record(action: str, *, project_id: str | None = None, target: str = "",
           outcome: str = "ok", **detail) -> None:
    """Record one event. Never raises: an audit write must not fail the migration."""
    event = AuditEvent(
        action=action,
        actor=current_actor(),
        at=datetime.now(timezone.utc).isoformat(),
        project_id=project_id or None,
        target=target,
        outcome=outcome,
        detail={k: v for k, v in detail.items() if v is not None},
    )
    log.info("audit %s", json.dumps(asdict(event), default=str))
    try:
        get_audit_store().append(event)
    except Exception as exc:
        log.warning("Could not persist audit event %s: %s", action, exc)
