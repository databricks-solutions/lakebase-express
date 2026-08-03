"""In-memory registry of data-migration runs + background execution.

A run streams each selected table on a daemon thread and updates a shared
RunState the API polls. State lives in process memory — fine for a single-user
accelerator App; swap for a Lakebase table or Redis to make it multi-worker.
"""
from __future__ import annotations

import logging
import threading
import uuid

from backend.connectors.factory import build_connector
from backend.connectors.lakebase import LakebaseConnection
from backend.migration.data_loader import capture_and_drop_fks, load_table, restore_fks
from backend.migration.models import DataLoadRequest, RunState, TableProgress
from backend.schema_migration.naming import map_object, map_schema

log = logging.getLogger("lakebase_express.runs")

_RUNS: dict[str, RunState] = {}
_LOCK = threading.Lock()


def get_run(run_id: str) -> RunState | None:
    with _LOCK:
        state = _RUNS.get(run_id)
        # Return a copy so the caller never observes a half-mutated object.
        return state.model_copy(deep=True) if state else None


def start_run(req: DataLoadRequest) -> str:
    run_id = uuid.uuid4().hex[:12]
    state = RunState(
        run_id=run_id,
        status="running",
        tables=[
            TableProgress(
                name=f"{t.schema_name}.{t.table_name}",
                target=f"{map_schema(t.schema_name, req.target_schema, req.identifier_case)}."
                       f"{map_object(t.target_table or t.table_name, req.identifier_case)}",
                total_rows=t.total_rows,
            )
            for t in req.tables
        ],
    )
    with _LOCK:
        _RUNS[run_id] = state
    threading.Thread(target=_execute, args=(run_id, req), daemon=True).start()
    return run_id


def _set(run_id: str, mutate) -> None:
    with _LOCK:
        state = _RUNS.get(run_id)
        if state:
            mutate(state)


def _execute(run_id: str, req: DataLoadRequest) -> None:
    try:
        source = build_connector(
            req.source_type,
            host=req.host, database=req.database, username=req.username,
            password=req.password, port=req.port,
        )
        target = LakebaseConnection(**req.lakebase.conn_kwargs())

        # FKs on/into the targets would fail the TRUNCATEs and slow the COPYs —
        # drop them for the load and restore them afterwards. (The plan's
        # post-data phase re-applies its own FKs idempotently on top.)
        fq_targets = [
            f'"{map_schema(t.schema_name, req.target_schema, req.identifier_case)}"'
            f'."{map_object(t.target_table or t.table_name, req.identifier_case)}"'
            for t in req.tables
        ]
        dropped_fks = capture_and_drop_fks(target, fq_targets)

        any_failed = False
        for i, spec in enumerate(req.tables):
            _set(run_id, lambda s, i=i: setattr(s.tables[i], "status", "running"))

            def progress(n: int, i=i) -> None:
                _set(run_id, lambda s: setattr(s.tables[i], "rows_copied", n))

            try:
                total = load_table(
                    source, target, spec, req.target_schema,
                    req.truncate_first, req.batch_size, progress,
                    identifier_case=req.identifier_case,
                )
                _set(run_id, lambda s, i=i, total=total: (
                    setattr(s.tables[i], "rows_copied", total),
                    setattr(s.tables[i], "status", "success"),
                ))
            except Exception as exc:
                any_failed = True
                log.warning("Load failed for %s: %s", spec.table_name, exc)
                _set(run_id, lambda s, i=i, exc=exc: (
                    setattr(s.tables[i], "status", "failed"),
                    setattr(s.tables[i], "error", str(exc)),
                ))

        restore_failures = restore_fks(target, dropped_fks)
        if restore_failures:
            any_failed = True
            msg = "Some foreign keys could not be restored after the load: " + "; ".join(
                restore_failures
            )
            _set(run_id, lambda s, msg=msg: setattr(s, "error", msg))

        final = "partial" if any_failed else "success"
        _set(run_id, lambda s, final=final: setattr(s, "status", final))
    except Exception as exc:  # setup-level failure (e.g. bad connection)
        log.exception("Run %s failed during setup", run_id)
        _set(run_id, lambda s, exc=exc: (setattr(s, "status", "failed"), setattr(s, "error", str(exc))))
