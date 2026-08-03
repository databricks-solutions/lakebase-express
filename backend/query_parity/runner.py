"""In-memory registry of query-parity runs + background execution.

Same pattern as backend/validation/runs.py: a daemon thread runs each query pair
against the source and the Lakebase target, compares the two results, and updates
a shared state the API polls. Process-memory state is fine for a single-user
accelerator App.

Every query is guarded read-only before it executes on either side — the AI is
told to emit only SELECTs, but the runner never trusts that: anything that isn't
a lone SELECT/WITH is rejected without touching a database.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid

from backend.connectors.credentials import LAKEBASE_NAMESPACE, remember_effective
from backend.connectors.factory import build_connector
from backend.connectors.lakebase import LakebaseConnection
from backend.query_parity.comparator import VALUE_SAMPLE, compare, side_result
from backend.query_parity.models import (
    ParityStatus,
    QueryParityReport,
    QueryParityRunRequest,
    QueryParityRunState,
    SideResult,
    SyntheticQuery,
)
from datetime import datetime, timezone

log = logging.getLogger("lakebase_express.query_parity.runner")

_RUNS: dict[str, QueryParityRunState] = {}
_LOCK = threading.Lock()

# Per-query timeouts. Synthetic parity queries are meant to be small (the prompt
# asks for LIMIT/TOP), so keep both sides snappy and treat a slow query as a
# finding rather than blocking the whole run.
_SOURCE_TIMEOUT_SECONDS = 120
_TARGET_TIMEOUT_MS = 120_000

# A read-only query is a single statement that begins with SELECT or a WITH…SELECT
# CTE. We strip leading comments, then require the first keyword to be SELECT/WITH
# and reject any embedded statement terminator followed by more SQL.
_LEADING_COMMENT_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)+", re.DOTALL)
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|CALL|INTO)\b",
    re.IGNORECASE,
)


def is_read_only(sql: str) -> bool:
    """Whether ``sql`` is a single read-only SELECT/WITH statement.

    Conservative by design: rejects multi-statement scripts and any write/DDL
    keyword. ``SELECT … INTO`` (T-SQL table materialization) is a write, so
    ``INTO`` is in the deny list.
    """
    text = _LEADING_COMMENT_RE.sub("", sql or "").strip()
    if not text:
        return False
    # Collapse to one trailing terminator; anything after it is a second statement.
    stripped = text.rstrip().rstrip(";").strip()
    if ";" in stripped:
        return False
    if not re.match(r"(?is)^(SELECT|WITH)\b", stripped):
        return False
    return _WRITE_KEYWORDS.search(stripped) is None


def get_run(run_id: str) -> QueryParityRunState | None:
    with _LOCK:
        state = _RUNS.get(run_id)
        # Return a copy so the caller never observes a half-mutated object.
        return state.model_copy(deep=True) if state else None


def start_run(req: QueryParityRunRequest) -> str:
    run_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _RUNS[run_id] = QueryParityRunState(run_id=run_id, queries_total=len(req.queries))
    threading.Thread(target=_execute, args=(run_id, req), daemon=True).start()
    return run_id


def _set(run_id: str, mutate) -> None:
    with _LOCK:
        state = _RUNS.get(run_id)
        if state:
            mutate(state)


def _run_source(source, sql: str) -> tuple[SideResult, list[dict]]:
    if not is_read_only(sql):
        return SideResult(error="Query is not read-only — only SELECT statements are run."), []
    start = time.monotonic()
    try:
        rows = source.query(sql, timeout=_SOURCE_TIMEOUT_SECONDS)
    except Exception as exc:
        return SideResult(error=str(exc)), []
    ms = int((time.monotonic() - start) * 1000)
    return side_result(rows, ms), rows


def _run_target(target: LakebaseConnection, sql: str) -> tuple[SideResult, list[dict]]:
    if not is_read_only(sql):
        return SideResult(error="Query is not read-only — only SELECT statements are run."), []
    start = time.monotonic()
    try:
        rows = target.query(sql, statement_timeout_ms=_TARGET_TIMEOUT_MS)
    except Exception as exc:
        return SideResult(error=str(exc)), []
    ms = int((time.monotonic() - start) * 1000)
    return side_result(rows, ms), rows


def _build_report(req: QueryParityRunRequest, comparisons: list, source_db: str, target_db: str) -> QueryParityReport:
    matched = sum(1 for c in comparisons if c.status is ParityStatus.MATCH)
    mismatched = sum(1 for c in comparisons if c.status is ParityStatus.MISMATCH)
    errored = sum(1 for c in comparisons if c.status is ParityStatus.ERROR)
    total = len(comparisons)
    score = int(100 * matched / total) if total else 100
    both_ran = [c for c in comparisons if c.source.ok and c.target.ok]
    return QueryParityReport(
        source_database=source_db,
        target_database=target_db,
        target_schema=req.target_schema,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        requested=len(req.queries),
        total=total,
        matched=matched,
        mismatched=mismatched,
        errored=errored,
        parity_score=score,
        source_total_ms=sum(c.source.duration_ms for c in both_ran),
        target_total_ms=sum(c.target.duration_ms for c in both_ran),
        comparisons=comparisons,
    )


def _execute(run_id: str, req: QueryParityRunRequest) -> None:
    try:
        source = build_connector(
            req.source.source_type,
            host=req.source.host, database=req.source.database,
            username=req.source.username, password=req.source.password,
            port=req.source.port,
        )
        target = LakebaseConnection(**req.lakebase.conn_kwargs())

        comparisons = []
        total = len(req.queries)
        for done, q in enumerate(req.queries):
            _set(run_id, lambda s, done=done, q=q: (
                setattr(s, "phase", "Running queries"),
                setattr(s, "queries_done", done),
                setattr(s, "queries_total", total),
                setattr(s, "current", q.title or q.id),
            ))
            src_res, src_rows = _run_source(source, q.source_sql)
            tgt_res, tgt_rows = _run_target(target, q.target_sql)
            comparisons.append(compare(
                q, src_res, tgt_res,
                source_rows=src_rows[:VALUE_SAMPLE], target_rows=tgt_rows[:VALUE_SAMPLE],
            ))

        _set(run_id, lambda s: setattr(s, "phase", "Building report"))
        report = _build_report(req, comparisons,
                                getattr(source, "database", ""), target.database)

        # The run authenticated on both sides — keep the credentials for the
        # session (mirrors the validation runs). A secret_ref persists the
        # pointer instead of the value.
        remember_effective(req.source.source_type, req.source.host, req.source.database,
                            req.source.username, req.source.project_id,
                            req.source.password, req.source.secret_ref)
        remember_effective(LAKEBASE_NAMESPACE, req.lakebase.host, req.lakebase.database,
                            req.lakebase.user, req.lakebase.project_id,
                            req.lakebase.password, req.lakebase.secret_ref)

        _set(run_id, lambda s: (
            setattr(s, "status", "success"),
            setattr(s, "phase", "done"),
            setattr(s, "current", ""),
            setattr(s, "queries_done", total),
            setattr(s, "report", report),
        ))
    except Exception as exc:  # surfaced to the polling UI
        log.exception("Query-parity run %s failed", run_id)
        _set(run_id, lambda s: (setattr(s, "status", "failed"), setattr(s, "error", str(exc))))
