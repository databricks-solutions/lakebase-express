"""Compare the results of one query pair run against source and target.

Pure functions, kept separate from the runner for testability. Three axes:

  * **count**  — same number of rows returned;
  * **format** — same result shape: identical column count and, comparing
    case-insensitively (Postgres folds unquoted identifiers to lower case),
    the same ordered column names, plus the same ordered row *values* over a
    sampled prefix (normalized so cross-dialect numeric/date formatting doesn't
    read as a difference);
  * **performance** — the ratio of the two execution times.

When results disagree, the comparison also records *which* columns and rows
differ (``mismatch_columns`` / ``row_diffs``) and keeps a bounded preview of each
side's rows (``SideResult.preview_rows``), so the UI can show the two result sets
side by side and highlight exactly what changed instead of only saying "row
values differ".
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from backend.query_parity.models import (
    ParityStatus,
    QueryComparison,
    RowDiff,
    SideResult,
    SyntheticQuery,
)

# How many leading rows to compare value-by-value. Count parity already covers the
# full result; this catches data drift without holding huge results in memory.
VALUE_SAMPLE = 200

# How many leading rows of each side to keep for the side-by-side preview, and
# how long a single cell may be before it's clipped for display.
PREVIEW_ROWS = 100
MAX_CELL_CHARS = 300

# Cap the recorded per-row diffs so a wholly-different large result doesn't bloat
# the persisted report; the detail text still reports the true total.
MAX_DIFF_ROWS = 100


def _normalize_value(v: object) -> str:
    """Canonical string for a cell so equivalent values compare equal across
    dialects (Decimal vs int, datetime precision, trailing spaces on char cols)."""
    if v is None:
        return "∅"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, Decimal, float)):
        # Compare numbers by value, not representation: 10, 10.0 and 10.00 match.
        d = Decimal(str(v)).normalize()
        # normalize() renders integers in exponent form (1E+1); expand them.
        return format(d, "f")
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v).rstrip()


def _display(v: object) -> str:
    """Normalized cell value, clipped for display in the preview grid."""
    s = _normalize_value(v)
    return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "…"


def _column_names(rows: list[dict]) -> list[str]:
    """Ordered column names from a result set (dict key order is preserved)."""
    return list(rows[0].keys()) if rows else []


def side_result(rows: list[dict], duration_ms: int) -> SideResult:
    """Build the persisted per-side summary (with a bounded row preview)."""
    return SideResult(
        ok=True,
        row_count=len(rows),
        column_names=_column_names(rows),
        duration_ms=duration_ms,
        truncated=len(rows) > PREVIEW_ROWS,
        preview_rows=[[_display(v) for v in r.values()] for r in rows[:PREVIEW_ROWS]],
    )


def _row_cells(row: dict) -> list[str]:
    return [_display(v) for v in row.values()]


def _diff_rows(
    source_rows: list[dict], target_rows: list[dict], source_cols: list[str]
) -> tuple[list[RowDiff], list[str], int]:
    """Positional row-by-row diff over the sampled prefix (columns must align).

    Returns ``(row_diffs, mismatch_columns, value_diff_total)`` where
    ``value_diff_total`` is the true count of differing value-rows (the returned
    list may be capped at ``MAX_DIFF_ROWS``)."""
    diffs: list[RowDiff] = []
    mismatch_cols: list[str] = []
    seen_cols: set[str] = set()
    value_diff_total = 0
    overlap = min(len(source_rows), len(target_rows), VALUE_SAMPLE)

    for i in range(overlap):
        s_vals = [_normalize_value(v) for v in source_rows[i].values()]
        t_vals = [_normalize_value(v) for v in target_rows[i].values()]
        if s_vals == t_vals:
            continue
        value_diff_total += 1
        diff_cols = []
        for j in range(min(len(s_vals), len(t_vals))):
            if s_vals[j] != t_vals[j]:
                name = source_cols[j] if j < len(source_cols) else f"column {j + 1}"
                diff_cols.append(name)
                if name not in seen_cols:
                    seen_cols.add(name)
                    mismatch_cols.append(name)
        if len(diffs) < MAX_DIFF_ROWS:
            diffs.append(RowDiff(
                row_index=i, kind="value",
                source_cells=_row_cells(source_rows[i]),
                target_cells=_row_cells(target_rows[i]),
                diff_columns=diff_cols,
            ))

    # Rows only one side returned (the counts differ) — show them so the extra or
    # missing rows are visible, not just counted.
    for i in range(overlap, min(len(source_rows), VALUE_SAMPLE)):
        if len(diffs) >= MAX_DIFF_ROWS:
            break
        diffs.append(RowDiff(row_index=i, kind="source_only", source_cells=_row_cells(source_rows[i])))
    for i in range(overlap, min(len(target_rows), VALUE_SAMPLE)):
        if len(diffs) >= MAX_DIFF_ROWS:
            break
        diffs.append(RowDiff(row_index=i, kind="target_only", target_cells=_row_cells(target_rows[i])))

    return diffs, mismatch_cols, value_diff_total


def compare(
    query: SyntheticQuery,
    source: SideResult,
    target: SideResult,
    source_rows: list[dict] | None = None,
    target_rows: list[dict] | None = None,
) -> QueryComparison:
    """Combine the two side results (and their sampled rows) into a verdict."""
    comp = QueryComparison(query=query, source=source, target=target)

    if not (source.ok and target.ok):
        comp.status = ParityStatus.ERROR
        failed = "source and target" if not source.ok and not target.ok else \
                 "source" if not source.ok else "target"
        comp.detail = f"Query failed on the {failed}."
        return comp

    comp.count_match = source.row_count == target.row_count
    same_col_count = len(source.column_names) == len(target.column_names)
    same_col_names = [c.lower() for c in source.column_names] == \
                     [c.lower() for c in target.column_names]

    # Row-level diff (only meaningful when columns align positionally).
    value_diff_total = 0
    if source_rows is not None and target_rows is not None and same_col_count:
        row_diffs, mismatch_cols, value_diff_total = _diff_rows(
            source_rows, target_rows, source.column_names
        )
    else:
        row_diffs, mismatch_cols = [], []

    values_match = True
    if comp.count_match and same_col_count and source_rows is not None and target_rows is not None:
        values_match = value_diff_total == 0
    comp.format_match = same_col_count and same_col_names and values_match

    if source.duration_ms > 0:
        comp.speedup_ratio = round(target.duration_ms / source.duration_ms, 3)

    if comp.count_match and comp.format_match:
        comp.status = ParityStatus.MATCH
        comp.detail = f"Identical results — {source.row_count:,} row" \
                      f"{'' if source.row_count == 1 else 's'} on both sides."
        return comp

    comp.status = ParityStatus.MISMATCH
    comp.mismatch_columns = mismatch_cols
    comp.row_diffs = row_diffs
    problems: list[str] = []
    if not comp.count_match:
        problems.append(f"row counts differ (source {source.row_count:,}, target {target.row_count:,})")
    if not same_col_count:
        problems.append(f"column counts differ (source {len(source.column_names)}, "
                        f"target {len(target.column_names)})")
    elif not same_col_names:
        problems.append("column names differ")
    if value_diff_total:
        compared = min(source.row_count, target.row_count, VALUE_SAMPLE)
        msg = f"{value_diff_total} of {compared} compared row" \
              f"{'' if compared == 1 else 's'} differ in value"
        if mismatch_cols:
            msg += f" (columns: {', '.join(mismatch_cols)})"
        problems.append(msg)
    comp.detail = "Results disagree: " + "; ".join(problems) + "."
    return comp
