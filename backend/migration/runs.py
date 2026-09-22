"""Data-migration runs + background execution.

A run streams each selected table on a daemon thread and updates a RunState the
API polls, persisted through backend/run_registry.py.

A run whose loader is gone — it failed, or the app was restarted mid-load — can be
resumed rather than restarted: the tables it already loaded are marked skipped and
only the rest are copied. Each table is loaded in a single transaction (see
data_loader), so it is either fully in or not at all — which is what makes a
table-level checkpoint exact.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone

from backend.connectors.factory import build_connector
from backend.connectors.lakebase import LakebaseConnection
from backend.migration.data_loader import capture_and_drop_fks, load_table, restore_fks
from backend.migration.models import (
    DataLoadRequest,
    RunState,
    TableLoadSpec,
    TableProgress,
)
from backend.run_registry import RunRegistry
from backend.schema_migration.naming import map_object, map_schema

log = logging.getLogger("lakebase_express.runs")

_REGISTRY: RunRegistry[RunState] = RunRegistry("sync_run", RunState)

# Statuses that mean a table needs no further work.
_DONE = frozenset({"success", "skipped"})
_TERMINAL = frozenset({"success", "failed", "partial"})

HEARTBEAT_SECONDS = 30.0
# Three missed beats: the loader is gone (restart, worker recycle), not just slow.
STALE_AFTER_SECONDS = 90.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_run(run_id: str) -> RunState | None:
    return _REGISTRY.get(run_id)


# --- Resume ----------------------------------------------------------------------


def tables_left(state: RunState) -> int:
    return sum(t.status not in _DONE for t in state.tables)


def is_resumable(state: RunState) -> bool:
    """True once the run's loader is gone — terminal, or running with a stale
    heartbeat — and there is still something to do. A live run is never resumable:
    two loaders on one table would fight over the same TRUNCATE."""
    if state.status not in _TERMINAL and not _is_stale(state):
        return False
    return tables_left(state) > 0 or bool(state.dropped_fks)


def find_resumable(project_id: str, limit: int = 20) -> RunState | None:
    """The newest resumable run of a project, for the UI's resume offer.

    A run that a later one already resumed is never it, however much of it failed —
    what it left behind is that later run's business now. Walking newest-first means
    every resume is seen before the run it continued.
    """
    superseded: set[str] = set()
    for record in _REGISTRY.list(limit, project_id):
        state = _REGISTRY.get(record.run_id)
        if state is None:
            continue
        if state.resumed_from:
            superseded.add(state.resumed_from)
        if state.run_id not in superseded and is_resumable(state):
            return state
    return None


def _is_stale(state: RunState) -> bool:
    beat = state.heartbeat_at or state.started_at
    if not beat:
        return True
    try:
        last = datetime.fromisoformat(beat)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() > STALE_AFTER_SECONDS


def _heartbeat(run_id: str, stop: threading.Event) -> None:
    """Proof of life while the loader runs, so an abandoned run can be told from one
    working through a slow table (which reports no progress for minutes)."""
    while not stop.wait(HEARTBEAT_SECONDS):
        _set(run_id, lambda s: setattr(s, "heartbeat_at", _now()))


def _seed(spec: TableLoadSpec, req: DataLoadRequest, loaded: dict[str, TableProgress]) -> TableProgress:
    name = f"{spec.schema_name}.{spec.table_name}"
    progress = TableProgress(
        name=name,
        target=f"{map_schema(spec.schema_name, req.target_schema, req.identifier_case)}."
               f"{map_object(spec.target_table or spec.table_name, req.identifier_case)}",
        total_rows=spec.total_rows,
    )
    done = loaded.get(name)
    if done is None:
        return progress
    # Already in the target from the run being resumed; its row count and timings
    # carry over so the totals still add up.
    return progress.model_copy(update={
        "status": "skipped",
        "rows_copied": done.rows_copied,
        "started_at": done.started_at,
        "finished_at": done.finished_at,
    })


def _merge_fks(inherited, captured) -> list[tuple[str, str, str]]:
    """One entry per (table, constraint); a freshly captured definition wins."""
    merged = {(t, n): (t, n, d) for t, n, d in inherited}
    merged.update({(t, n): (t, n, d) for t, n, d in captured})
    return list(merged.values())


# --- Execution -------------------------------------------------------------------


def start_run(req: DataLoadRequest) -> str:
    prior = get_run(req.resume_from) if req.resume_from else None
    loaded = {t.name: t for t in prior.tables if t.status in _DONE} if prior else {}
    run_id = str(uuid.uuid4())
    state = RunState(
        run_id=run_id,
        project_id=req.project_id,
        status="running",
        started_at=_now(),
        heartbeat_at=_now(),
        resumed_from=prior.run_id if prior else None,
        # FKs the resumed run dropped are already gone from the target, so nothing
        # captures them now — carried over or they stay dropped for good.
        dropped_fks=list(prior.dropped_fks) if prior else [],
        tables=[_seed(t, req, loaded) for t in req.tables],
    )
    _REGISTRY.create(state, req.project_id)
    threading.Thread(target=_execute, args=(run_id, req), daemon=True).start()
    return run_id


def _set(run_id: str, mutate) -> None:
    _REGISTRY.update(run_id, mutate)


def _execute(run_id: str, req: DataLoadRequest) -> None:
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(run_id, stop), daemon=True).start()
    try:
        _copy_tables(run_id, req)
    finally:
        stop.set()


def _copy_tables(run_id: str, req: DataLoadRequest) -> None:
    # The seeded state says what a resume already has, so it drives the skipping.
    seeded = _REGISTRY.get(run_id)
    skip = {i for i, t in enumerate(seeded.tables) if t.status == "skipped"} if seeded else set()
    inherited = list(seeded.dropped_fks) if seeded else []
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
        dropped_fks = _merge_fks(inherited, capture_and_drop_fks(target, fq_targets))
        # Persisted before the load: without this an interrupted run takes the only
        # copy of these definitions with it and the target keeps no FKs at all.
        _set(run_id, lambda s, fks=dropped_fks: setattr(s, "dropped_fks", fks))

        any_failed = False
        for i, spec in enumerate(req.tables):
            if i in skip:
                continue
            _set(run_id, lambda s, i=i: (
                setattr(s.tables[i], "status", "running"),
                setattr(s.tables[i], "started_at", _now()),
            ))

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
                    setattr(s.tables[i], "finished_at", _now()),
                ))
            except Exception as exc:
                any_failed = True
                log.warning("Load failed for %s: %s", spec.table_name, exc)
                _set(run_id, lambda s, i=i, exc=exc: (
                    setattr(s.tables[i], "status", "failed"),
                    setattr(s.tables[i], "error", str(exc)),
                    setattr(s.tables[i], "finished_at", _now()),
                ))

        restore_failures = restore_fks(target, dropped_fks)
        if restore_failures:
            any_failed = True
            msg = "Some foreign keys could not be restored after the load: " + "; ".join(
                restore_failures
            )
            _set(run_id, lambda s, msg=msg: setattr(s, "error", msg))
        # Restored, or reported above — either way a resume of this run must not
        # try again; the plan's idempotent FK items are the backstop.
        _set(run_id, lambda s: setattr(s, "dropped_fks", []))

        final = "partial" if any_failed else "success"
        _set(run_id, lambda s, final=final: (
            setattr(s, "status", final),
            setattr(s, "finished_at", _now()),
        ))
    except Exception as exc:  # setup-level failure (e.g. bad connection)
        log.exception("Run %s failed during setup", run_id)
        _set(run_id, lambda s, exc=exc: (
            setattr(s, "status", "failed"),
            setattr(s, "error", str(exc)),
            setattr(s, "finished_at", _now()),
        ))
