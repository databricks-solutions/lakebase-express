"""Post-migration maintenance: ``VACUUM (ANALYZE)`` on every loaded table.

A bulk-loaded table has no planner statistics until autovacuum gets to it, and no
visibility map, so index-only scans fall back to heap fetches. The last step of a
sync run fixes both, before the first real query. Runs on a daemon thread (a
large table takes minutes, past the Apps ~120s request timeout) and the UI polls
``MaintenanceRunState``.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from backend import audit
from backend.connectors.lakebase import LakebaseConnection, transient_reason
from backend.migration.models import MaintenanceRequest, MaintenanceRunState, MaintenanceTable
from backend.retry import DB_POLICY, call_with_retry
from backend.run_registry import RunRegistry
from backend.schema_migration.naming import map_object, map_schema

log = logging.getLogger("lakebase_express.maintenance")

_REGISTRY: RunRegistry[MaintenanceRunState] = RunRegistry("maintenance", MaintenanceRunState)

# What the step runs per table; shared with the async job's notebook.
MAINTENANCE_SQL = "VACUUM (ANALYZE) {table}"

ProgressFn = Callable[[int, MaintenanceTable], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def target_tables(req: MaintenanceRequest) -> list[tuple[str, str]]:
    """(schema, table) on the target for each requested source table."""
    return [
        (map_schema(t.schema_name, req.target_schema, req.identifier_case),
         map_object(t.target_table or t.table_name, req.identifier_case))
        for t in req.tables
    ]


def maintain_tables(
    target: LakebaseConnection, tables: list[tuple[str, str]], on_done: ProgressFn | None = None
) -> list[MaintenanceTable]:
    """VACUUM (ANALYZE) each table on one autocommit connection (VACUUM can't run
    in a transaction). A failure is recorded and the rest carry on."""
    results: list[MaintenanceTable] = []
    warnings: list[str] = []
    conn = None

    def open_conn():
        c = target.connect()
        c.autocommit = True
        # VACUUM skips a table it lacks rights on with only a WARNING; keep it.
        c.add_notice_handler(
            lambda d: warnings.append(d.message_primary)
            if d.severity_nonlocalized == "WARNING" else None
        )
        with c.cursor() as cur:
            # A server default would cut a big table's VACUUM short.
            cur.execute("SET statement_timeout = 0")
        return c

    def reconnect() -> None:
        nonlocal conn
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        conn = open_conn()

    try:
        conn = open_conn()
        for i, (schema, table) in enumerate(tables):
            name = f"{schema}.{table}"
            sql = MAINTENANCE_SQL.format(table=f"{_pg_ident(schema)}.{_pg_ident(table)}")
            warnings.clear()
            t0 = time.perf_counter()

            def run(sql=sql) -> None:
                with conn.cursor() as cur:
                    cur.execute(sql)

            try:
                call_with_retry(run, policy=DB_POLICY, transient=transient_reason,
                                what=f"maintain {name}", log=log, before_retry=reconnect)
                item = MaintenanceTable(
                    name=name, status="failed" if warnings else "success",
                    error="; ".join(warnings) or None,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
            except Exception as exc:
                log.warning("Maintenance failed for %s: %s", name, exc)
                item = MaintenanceTable(name=name, status="failed", error=str(exc),
                                        duration_ms=int((time.perf_counter() - t0) * 1000))
                if conn is None or conn.closed or conn.broken:
                    reconnect()
            results.append(item)
            if on_done:
                on_done(i, item)
    finally:
        if conn is not None:
            conn.close()
    return results


# --- Background run ----------------------------------------------------------------


def get_run(run_id: str) -> MaintenanceRunState | None:
    return _REGISTRY.get(run_id)


def start_run(req: MaintenanceRequest) -> str:
    run_id = str(uuid.uuid4())
    tables = target_tables(req)
    state = MaintenanceRunState(
        run_id=run_id, status="running", started_at=_now(),
        tables=[MaintenanceTable(name=f"{s}.{t}") for s, t in tables],
    )
    _REGISTRY.create(state, req.project_id)
    audit.spawn(_execute, run_id, req, tables)
    return run_id


def _set(run_id: str, mutate) -> None:
    _REGISTRY.update(run_id, mutate)


def _execute(run_id: str, req: MaintenanceRequest, tables: list[tuple[str, str]]) -> None:
    try:
        target = LakebaseConnection(**req.lakebase.conn_kwargs())

        def done(i: int, item: MaintenanceTable) -> None:
            _set(run_id, lambda s: s.tables.__setitem__(i, item))

        results = maintain_tables(target, tables, done)
        final = "partial" if any(r.status == "failed" for r in results) else "success"
        _set(run_id, lambda s: (setattr(s, "status", final), setattr(s, "finished_at", _now())))
    except Exception as exc:  # setup-level failure, e.g. no connection
        log.exception("Maintenance run %s failed", run_id)
        _set(run_id, lambda s, exc=exc: (
            setattr(s, "status", "failed"), setattr(s, "error", str(exc)),
            setattr(s, "finished_at", _now()),
        ))
