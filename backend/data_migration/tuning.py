"""Recommends the snapshot load's tuning from the assessment stats and the live limits.

Pure: the limits are probed separately (capacity.py), so the same inputs always
render the same notebook. Overrides win over every recommendation.
"""
from __future__ import annotations

import math

from backend.data_migration.models import (
    FETCH_RANGE,
    CapacityLimits,
    TablePlan,
    TableRef,
    TuningOverrides,
    TuningPlan,
)

_MIB = 1024 * 1024

CHUNK_BYTES = 256 * _MIB         # per key-range chunk — what one task holds, whatever the streams
CHUNK_ROWS = 2_000_000           # the same, when the scan has no table size
MAX_CHUNKS = 10_000
MAX_TABLE_STREAMS = 32
DEFAULT_CHUNKS = 8               # the old fixed partitions, when the table size is unknown
FETCH_BYTES = 8 * _MIB           # per JDBC round trip
(FETCH_MIN, FETCH_MAX), FETCH_DEFAULT = FETCH_RANGE, 10_000
TARGET_SHARE = 0.75              # of the target's free connections
STREAMS_PER_CPU = 4
WORKER_SHARE = 0.5               # of the source's worker threads
DEFAULT_STREAMS = 32             # the old fixed 4 tables x 8 partitions
MAX_STREAMS = 64
MAX_TABLES = 16

# Leading-key types a table can be split into key ranges on.
_PARTITIONABLE_TYPES = {
    "tinyint", "smallint", "int", "bigint", "decimal", "numeric", "money", "smallmoney",
    "float", "real", "date", "datetime", "datetime2", "smalldatetime",
}


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _target_free(c: CapacityLimits | None) -> int | None:
    if c is None or c.target_max_connections is None:
        return None
    return max(0, c.target_max_connections - (c.target_reserved_connections or 0)
               - (c.target_connections_in_use or 0))


def _target_budget(c: CapacityLimits | None) -> tuple[int | None, str | None]:
    free = _target_free(c)
    if free is None:
        return None, None
    budget = max(1, math.floor(free * TARGET_SHARE))
    return budget, (
        f"Target allows {c.target_max_connections} connections "
        f"({c.target_reserved_connections or 0} reserved, {c.target_connections_in_use or 0} in use) "
        f"→ {budget} streams ({TARGET_SHARE:.0%} of the free ones)."
    )


def _source_budget(c: CapacityLimits | None) -> tuple[int | None, str | None]:
    if c is None or not (c.source_cpus or c.source_max_workers):
        return None, None
    caps, basis = [], []
    if c.source_cpus:
        caps.append(max(2, round(c.source_cpus * STREAMS_PER_CPU)))
        basis.append(f"{c.source_cpus:g} vCores × {STREAMS_PER_CPU}")
    if c.source_max_workers:
        caps.append(max(1, math.floor(c.source_max_workers * WORKER_SHARE)))
        basis.append(f"{WORKER_SHARE:.0%} of {c.source_max_workers} workers")
    budget = min(caps)
    slo = f"{c.source_service_objective} · " if c.source_service_objective else ""
    return budget, (
        f"Source {slo}{' / '.join(basis)} → {budget} streams, leaving room for its own workload."
    )


def _streams(c: CapacityLimits | None, o: TuningOverrides) -> tuple[int, list[str], list[str]]:
    target, t_reason = _target_budget(c)
    source, s_reason = _source_budget(c)
    reasons = [r for r in (t_reason, s_reason) if r]
    warnings: list[str] = []
    if o.max_streams is not None:
        free = _target_free(c)
        if free is not None and o.max_streams > free:
            warnings.append(
                f"{o.max_streams} streams is more than the target's {free} free connections — "
                "the load can fail with 'too many connections'."
            )
        if source is not None and o.max_streams > source:
            warnings.append(
                f"{o.max_streams} streams is above the {source} recommended for the source, "
                "which can slow its own workload during the load."
            )
        return o.max_streams, [*reasons, f"Streams set by the project: {o.max_streams}."], warnings
    known = [b for b in (target, source) if b is not None]
    if not known:
        return DEFAULT_STREAMS, [
            f"Source and target limits unknown — using the previous default of {DEFAULT_STREAMS} streams."
        ], warnings
    if target is None:
        reasons.append("Target limit unknown — sized from the source alone.")
    if source is None:
        reasons.append("Source limits unknown — sized from the target alone.")
    if min(known) > MAX_STREAMS:
        reasons.append(f"Capped at {MAX_STREAMS} streams.")
    return min(*known, MAX_STREAMS), reasons, warnings


def _partitionable(t: TableRef) -> str | None:
    """None when range reads are possible, else why not."""
    if not t.primary_key:
        return "no primary key"
    kind = t.primary_key_type.lower()
    if kind and kind not in _PARTITIONABLE_TYPES:
        return f"{kind} key"
    return None


def _chunks(t: TableRef) -> tuple[int, str]:
    """How many key-range chunks the table is read in, and why."""
    why_not = _partitionable(t)
    if why_not:
        return 1, f"{why_not} → one chunk"
    if t.data_bytes:
        n = min(MAX_CHUNKS, max(1, math.ceil(t.data_bytes / CHUNK_BYTES)))
        return n, f"{_size(t.data_bytes)} → {n:,} chunk{'s' if n > 1 else ''} of ~{_size(t.data_bytes // n)}"
    if t.row_count is None:
        return DEFAULT_CHUNKS, f"size unknown → {DEFAULT_CHUNKS} chunks"
    if not t.row_count:
        return 1, "empty → one chunk"
    n = min(MAX_CHUNKS, math.ceil(t.row_count / CHUNK_ROWS))
    return n, f"{t.row_count:,} rows → {n:,} chunk{'s' if n > 1 else ''}"


def _table_streams(chunks: int, cap: int, o: TuningOverrides) -> tuple[int, str]:
    """Chunks of one table loading at once — never more than it has."""
    if o.table_streams is not None:
        n = min(o.table_streams, cap, chunks)
        return n, f"{n} at a time (set by the project)"
    n = min(chunks, MAX_TABLE_STREAMS, cap)
    return n, f"{n} at a time"


def _fetch(t: TableRef, o: TuningOverrides) -> tuple[int, str]:
    if o.fetch_size is not None:
        return o.fetch_size, "fetch set by the project"
    if not (t.data_bytes and t.row_count):
        return FETCH_DEFAULT, "default fetch"
    width = t.data_bytes / t.row_count
    n = max(FETCH_MIN, min(FETCH_MAX, int(FETCH_BYTES / width) // 500 * 500))
    return n, f"~{_size(round(width))}/row → {n:,} rows per fetch"


def recommend(
    tables: list[TableRef], capacity: CapacityLimits | None, overrides: TuningOverrides
) -> TuningPlan:
    streams, reasons, warnings = _streams(capacity, overrides)
    ordered = sorted(tables, key=lambda t: (t.data_bytes, t.row_count or 0), reverse=True)
    # Half the streams at most per table, so one big table never holds up the rest.
    cap = streams if len(tables) == 1 else max(1, math.ceil(streams / 2))
    plans = []
    for t in ordered:
        chunks, c_reason = _chunks(t)
        table_streams, s_reason = _table_streams(chunks, cap, overrides)
        fetch, f_reason = _fetch(t, overrides)
        plans.append(TablePlan(
            schema_name=t.schema_name, table_name=t.table_name, chunks=chunks,
            streams=table_streams, fetch_size=fetch,
            reason=f"{c_reason}, {s_reason} · {f_reason}",
        ))
    if overrides.table_streams is not None and overrides.table_streams > cap:
        warnings.append(
            f"{overrides.table_streams} streams per table is more than one table may use "
            f"({cap} of {streams} streams) — capped at {cap}."
        )
    if overrides.table_streams is not None:
        reasons.append(f"Streams per table set by the project: {overrides.table_streams}.")
    if overrides.fetch_size is not None:
        reasons.append(f"Rows per fetch set by the project: {overrides.fetch_size:,}.")
    # Every loading table holds at least one stream, so more tables than streams would just wait.
    parallel = max(1, min(len(tables), streams, MAX_TABLES))
    if capacity:
        reasons.extend(capacity.notes)
    return TuningPlan(
        max_streams=streams, max_parallel_tables=parallel, tables=plans,
        reasons=reasons, warnings=warnings,
    )
