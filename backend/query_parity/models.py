"""Post-migration query-parity contract.

An independent post-migration module: a Foundation Model generates a batch of
synthetic, read-only queries over the migrated schema — one in the source
dialect (T-SQL) and its PostgreSQL equivalent — and each pair is run against the
source and the Lakebase target. Every pair is then compared on three axes:

  * **count**       — same number of rows returned;
  * **format**      — same result shape (column count, names, ordered values);
  * **performance** — how the two execution times relate.

Mirrors the conventions of the other phase models (plain Pydantic, str-enums,
fail-soft AI results) and the frontend types in ``frontend/src/api.ts`` 1:1 —
keep the two in sync.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from backend.assessment.models import ConnectionRequest, TableInfo
from backend.migration.models import LakebaseConnRequest
from backend.schema_migration.naming import IdentifierCase


class ParityStatus(str, Enum):
    """Overall verdict for one compared query pair."""

    MATCH = "match"          # same row count and same result shape/values
    MISMATCH = "mismatch"    # ran on both sides but the results disagree
    ERROR = "error"          # failed to run on one or both sides


class SyntheticQuery(BaseModel):
    """One generated query pair: the same intent in both dialects."""

    id: str
    title: str = ""                      # short human label, e.g. "Top customers by revenue"
    intent: str = ""                     # what the query exercises and why
    category: str = "read"               # aggregation | join | filter | window | read
    source_sql: str                      # T-SQL, run against the source
    target_sql: str                      # PostgreSQL, run against Lakebase


class SideResult(BaseModel):
    """Outcome of running one side of a pair against one database."""

    ok: bool = False
    row_count: int = 0
    column_names: list[str] = []
    duration_ms: int = 0
    truncated: bool = False              # preview shows only a prefix of the rows
    # A bounded sample of the result rows (display strings), so the UI can show
    # the two result sets side by side. Capped in rows, columns, and cell length.
    preview_rows: list[list[str]] = []
    error: str | None = None


class RowDiff(BaseModel):
    """One differing row in the compared prefix, with both sides' cells.

    ``row_index`` is 0-based within the sampled prefix. ``kind`` distinguishes a
    value disagreement (row present on both sides) from a row that only one side
    returned (when the counts differ). ``diff_columns`` names the columns whose
    values disagree (empty for source-only/target-only rows)."""

    row_index: int
    kind: str = "value"                  # value | source_only | target_only
    source_cells: list[str] = []         # [] when the row is target-only
    target_cells: list[str] = []         # [] when the row is source-only
    diff_columns: list[str] = []         # column names that differ (value rows)


class QueryComparison(BaseModel):
    """A generated query pair plus how source and target compared."""

    query: SyntheticQuery
    source: SideResult = Field(default_factory=SideResult)
    target: SideResult = Field(default_factory=SideResult)

    status: ParityStatus = ParityStatus.ERROR
    count_match: bool = False            # same number of rows on both sides
    format_match: bool = False           # same column count/names and ordered values
    # Performance: positive means the target was faster than the source. Ratio is
    # target_ms / source_ms (< 1 = target faster). Only meaningful when both ran.
    speedup_ratio: float | None = None
    detail: str = ""                     # human-readable summary of the comparison
    # Which columns/rows disagree, so the UI can name and highlight them instead
    # of just saying "row values differ". Populated only for mismatches.
    mismatch_columns: list[str] = []     # columns that disagree anywhere in the sample
    row_diffs: list[RowDiff] = []        # the differing rows in the compared prefix


class QueryParityReport(BaseModel):
    source_database: str = ""
    target_database: str = ""
    target_schema: str = ""
    generated_at: str = ""
    endpoint: str = ""                   # FM endpoint that generated the queries
    requested: int = 0                   # queries the user asked for
    total: int = 0                       # queries actually compared
    matched: int = 0
    mismatched: int = 0
    errored: int = 0
    parity_score: int = 100              # % of compared queries that fully matched
    # Aggregate performance over pairs that ran on both sides.
    source_total_ms: int = 0
    target_total_ms: int = 0
    comparisons: list[QueryComparison] = []


# --- Query generation (synchronous FM call) ----------------------------------------


class GenerateQueriesRequest(BaseModel):
    # The migrated tables (from the assessment report the SPA already holds), so
    # generation needs no live connection — same shape as the plan build.
    tables: list[TableInfo] = Field(..., min_length=1)
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE
    count: int = Field(5, ge=1, le=50)   # how many synthetic queries to generate
    endpoint: str | None = None          # FM endpoint override


class GenerateQueriesResponse(BaseModel):
    queries: list[SyntheticQuery] = []
    endpoint: str = ""
    success: bool = False
    error: str | None = None


# --- Run (start + poll, like the validation runs) ----------------------------------


class QueryParityRunRequest(BaseModel):
    source: ConnectionRequest
    lakebase: LakebaseConnRequest
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE
    # The queries to execute. Normally the ones just generated (so the user can
    # review/edit them first); the count is implied by the list length.
    queries: list[SyntheticQuery] = Field(..., min_length=1)


class QueryParityRunState(BaseModel):
    run_id: str
    status: str = "running"              # running|success|failed
    phase: str = "starting"              # human-readable current phase
    queries_total: int = 0
    queries_done: int = 0
    current: str = ""                    # title of the query being run
    report: QueryParityReport | None = None
    error: str | None = None
