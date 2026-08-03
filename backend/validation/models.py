"""Post-migration validation contract.

Mirrors the conventions of the other phase models: plain Pydantic models,
str-enums, and fail-soft AI results (``success`` + ``error`` instead of raising).
The frontend types in ``frontend/src/api.ts`` mirror these 1:1 — keep in sync.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from backend.assessment.models import ConnectionRequest, Severity
from backend.migration.models import LakebaseConnRequest, ObjectKind
from backend.schema_migration.naming import IdentifierCase


class MatchStatus(str, Enum):
    MATCHED = "matched"      # exists on both sides and agrees
    MISSING = "missing"      # exists in the source, not in the target
    MISMATCH = "mismatch"    # exists on both sides but differs (rows/structure)
    EXTRA = "extra"          # exists only in the target


class ValidationItem(BaseModel):
    """One compared object (schema/table/view/procedure/function/trigger)."""

    id: str                              # e.g. "table:SalesLT.Product"
    kind: ObjectKind
    source_name: str = ""                # "" for target-only (extra) items
    target_name: str
    status: MatchStatus
    severity: Severity = Severity.INFO
    detail: str = ""
    recommendation: str = ""

    # Tables only.
    source_rows: int | None = None
    target_rows: int | None = None
    rows_approximate: bool = False       # counts are planner estimates, not exact (huge tables)
    columns_missing: list[str] = []      # in the source, absent in the target
    columns_extra: list[str] = []        # in the target, absent in the source
    type_drift: list[str] = []           # "col: expected X, found Y" (normalized)

    # Remediation aids.
    fix_sql: str = ""                    # deterministic fix, when derivable
    source_definition: str = ""          # original T-SQL (feeds the AI fix)
    remediated: bool = False             # a fix was applied; re-run to verify


class ValidationReport(BaseModel):
    source_database: str
    target_database: str
    target_schema: str
    generated_at: str = ""
    match_score: int = 100               # % of compared objects that matched (100 = all match)
    total_source: int = 0                # source-side objects compared
    matched: int = 0
    missing: int = 0
    mismatched: int = 0
    extra: int = 0
    # Exact row totals over tables counted on BOTH sides — same table set for
    # both numbers, so they are directly comparable.
    source_rows: int = 0
    target_rows: int = 0
    tables_compared: int = 0             # tables with counts on both sides
    tables_estimated: int = 0            # of those, how many used approximate counts
    items: list[ValidationItem] = []


# --- Run (start + poll, like the data-load runs) ----------------------------------


class ValidationRunRequest(BaseModel):
    source: ConnectionRequest
    lakebase: LakebaseConnRequest
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE
    # "full" compares everything; "objects" re-checks only schemas and code
    # objects (no table structure or row counts) — the fast post-agent pass.
    scope: str = "full"
    # Count huge tables by planner estimate (fast, approximate) instead of an
    # exact COUNT(*). Default on; set False for exact counts on every table (with
    # a generous timeout — a full scan of a very large table can take minutes).
    use_estimates: bool = True
    # With scope="objects": the report the re-scan refreshes — its table items
    # (structure + row counts) carry over so the merged report stays complete.
    previous: ValidationReport | None = None


class ValidationRunState(BaseModel):
    run_id: str
    status: str = "running"              # running|success|failed
    phase: str = "starting"              # human-readable current phase
    tables_total: int = 0                # row-count comparison progress
    tables_done: int = 0
    current: str = ""                    # table currently being counted
    report: ValidationReport | None = None
    error: str | None = None


# --- AI fix proposal ---------------------------------------------------------------


class FixProposalRequest(BaseModel):
    item: ValidationItem
    target_schema: str = "public"
    endpoint: str | None = None          # FM endpoint override


class FixProposal(BaseModel):
    analysis: str = ""
    sql: str = ""
    endpoint: str = ""
    success: bool = False
    error: str | None = None


# --- AI repair agent (autonomous remediation) ---------------------------------------
#
# Moved here from the Create Sync module: the agent loop now starts from a
# validation report's inconsistencies instead of failed plan applies.


class RepairAttempt(BaseModel):
    attempt: int
    analysis: str = ""                  # the agent's diagnosis
    sql: str = ""                       # the fix it proposed and applied
    status: str = "applying"            # applying|success|failed|gave_up
    error: str | None = None            # apply error, feeding the next attempt


class RepairTarget(BaseModel):
    item: ValidationItem
    # Attempts from an earlier agent run — a re-run continues from where the
    # agent left off instead of repeating fixes it already tried.
    prior_attempts: list[RepairAttempt] = []


class ValidationRepairRequest(BaseModel):
    lakebase: LakebaseConnRequest
    targets: list[RepairTarget] = Field(..., min_length=1)
    target_schema: str = "public"
    endpoint: str | None = None          # FM endpoint override
    max_attempts: int = Field(3, ge=1, le=5)


class RepairItemState(BaseModel):
    id: str
    name: str
    kind: ObjectKind
    status: str = "pending"             # pending|analyzing|applying|success|failed
    gave_up: bool = False               # not resolvable by SQL (e.g. row-count drift)
    reason: str = ""                    # the inconsistency being resolved
    attempts: list[RepairAttempt] = []
    fixed_sql: str = ""                 # set on success, for the run report


class RepairState(BaseModel):
    run_id: str
    status: str = "running"             # running|success|partial|failed
    attempt: int = 0
    max_attempts: int = 3
    fixed: int = 0
    remaining: int = 0
    items: list[RepairItemState] = []
    error: str | None = None
