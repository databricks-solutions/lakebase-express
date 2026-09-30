"""Migration report contract — one project's audit cycle, in the order it happened.

Assessment → plan → result → validation → query parity, plus what the report
cannot vouch for. The frontend types in ``frontend/src/api.ts`` mirror these 1:1.
"""
from __future__ import annotations

from pydantic import BaseModel

from backend.assessment.models import AIAssessment

# Bumped when a field changes meaning.
REPORT_VERSION = "1"

# How much of the cycle an export covers. A section absent from the scope is None
# rather than empty, so "not part of this artifact" cannot read as "nothing found".
SCOPE_FULL = "full"
SCOPE_ASSESSMENT = "assessment"
SCOPES = (SCOPE_FULL, SCOPE_ASSESSMENT)


class ReportProvenance(BaseModel):
    """Who produced the report, from which project, and how much it saw."""

    generated_at: str
    project_id: str
    project_name: str
    report_version: str = REPORT_VERSION
    tool: str = "lakebase-express"
    tool_version: str = ""
    phase_statuses: dict[str, str] = {}
    # What the report cannot vouch for, in plain words — silence would read as success.
    completeness: list[str] = []


class Coordinates(BaseModel):
    """What moved where. Hosts and databases only: never a password or a secret value."""

    source_type: str = ""
    source_host: str = ""
    source_database: str = ""
    target_host: str = ""
    target_database: str = ""
    target_schema: str = ""
    identifier_case: str = ""


class Headline(BaseModel):
    """The numbers a reader checks first.

    Every score is ``None`` when its phase never ran, so "not measured" cannot be
    mistaken for a zero.
    """

    readiness_score: int | None = None
    tables: int = 0
    total_rows: int = 0
    programmable_objects: int = 0
    tables_selected: int = 0
    plan_items: int = 0
    rows_copied: int | None = None
    match_score: int | None = None
    parity_score: int | None = None


# --- Assessment -------------------------------------------------------------------


class FindingGroup(BaseModel):
    """One compatibility rule and the objects it fired on."""

    rule_id: str
    title: str
    severity: str
    detail: str = ""
    recommendation: str = ""
    affected: list[str] = []
    # Objects before the cap, so a trimmed list still states its real size.
    affected_total: int = 0


class TableRow(BaseModel):
    name: str
    rows: int = 0
    columns: int = 0
    primary_key: str = ""


class AssessmentSection(BaseModel):
    database: str = ""
    readiness_score: int = 100
    score_formula: str = ""
    severity_counts: dict[str, int] = {}
    table_count: int = 0
    total_rows: int = 0
    programmable_object_count: int = 0
    # PROCEDURE/VIEW/FUNCTION/TRIGGER -> how many were scanned.
    object_counts: dict[str, int] = {}
    largest_tables: list[TableRow] = []
    tables_total: int = 0
    findings: list[FindingGroup] = []
    findings_total: int = 0
    # The model's deep-dive, advisory and written before the plan existed.
    ai: AIAssessment | None = None


# --- Plan -------------------------------------------------------------------------


class CodeObjectRow(BaseModel):
    """One translated procedure, view, function, or trigger."""

    source: str
    target: str
    object_type: str = ""
    # What the SQL actually creates, which is not always the source kind.
    target_kind: str = ""
    provenance: str = "ai"     # ai | user-edited | not-translated
    note: str = ""


class PlanSection(BaseModel):
    total: int = 0
    by_kind: dict[str, int] = {}
    # Constraints, indexes, FKs and triggers are applied after the data load.
    pre_data: int = 0
    post_data: int = 0
    collations: list[str] = []
    code_objects: list[CodeObjectRow] = []
    code_objects_total: int = 0
    translated: int = 0
    user_edited: int = 0
    not_translated: int = 0


# --- Result -----------------------------------------------------------------------


class LoadTable(BaseModel):
    name: str
    target: str = ""
    status: str = ""
    rows_copied: int = 0
    total_rows: int = 0
    error: str = ""


class LoadRun(BaseModel):
    """One recorded run: a load this app streamed, or a Databricks job it provisioned."""

    kind: str                  # sync_run | async_run | async_job
    run_id: str
    status: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration: str = ""
    tables_total: int = 0
    tables_ok: int = 0
    tables_failed: int = 0
    tables_skipped: int = 0
    rows_copied: int = 0
    resumed_from: str = ""
    job_url: str = ""
    scheduled_cron: str = ""
    error: str = ""
    tables: list[LoadTable] = []


class ResultSection(BaseModel):
    """What the migration actually did, from persisted run state."""

    runs: list[LoadRun] = []
    runs_total: int = 0
    rows_copied: int = 0
    tables_loaded: int = 0
    # False when run history lives in process memory, i.e. a restart emptied it.
    history_persistent: bool = True


# --- Validation -------------------------------------------------------------------


class ValidationRow(BaseModel):
    """One object that did not match."""

    id: str
    name: str
    kind: str = ""
    status: str = ""
    severity: str = ""
    detail: str = ""
    recommendation: str = ""
    source_rows: int | None = None
    target_rows: int | None = None
    rows_approximate: bool = False
    columns_missing: list[str] = []
    columns_extra: list[str] = []
    type_drift: list[str] = []
    collation_drift: list[str] = []
    objects: list[str] = []


class ValidationSection(BaseModel):
    generated_at: str = ""
    match_score: int = 100
    total_source: int = 0
    matched: int = 0
    missing: int = 0
    mismatched: int = 0
    extra: int = 0
    source_rows: int = 0
    target_rows: int = 0
    # target - source over the tables counted on both sides, so nobody subtracts.
    row_delta: int = 0
    tables_compared: int = 0
    tables_estimated: int = 0
    remediated: int = 0
    outstanding: list[ValidationRow] = []
    outstanding_total: int = 0


# --- Query parity -----------------------------------------------------------------


class ParityRow(BaseModel):
    id: str
    title: str = ""
    category: str = ""
    intent: str = ""
    status: str = ""
    count_match: bool = False
    format_match: bool = False
    source_rows: int = 0
    target_rows: int = 0
    source_ms: int = 0
    target_ms: int = 0
    speedup_ratio: float | None = None
    detail: str = ""
    mismatch_columns: list[str] = []
    source_error: str = ""
    target_error: str = ""


class ParitySection(BaseModel):
    generated_at: str = ""
    endpoint: str = ""
    parity_score: int = 100
    requested: int = 0
    total: int = 0
    matched: int = 0
    mismatched: int = 0
    errored: int = 0
    source_total_ms: int = 0
    target_total_ms: int = 0
    # source_total_ms / target_total_ms over pairs that ran on both sides; > 1 = target faster.
    speedup: float | None = None
    queries: list[ParityRow] = []


class MigrationReport(BaseModel):
    """The exported artifact. A section is ``None`` when its phase never ran, or when
    ``scope`` leaves it out."""

    scope: str = SCOPE_FULL
    provenance: ReportProvenance
    coordinates: Coordinates
    headline: Headline
    assessment: AssessmentSection | None = None
    plan: PlanSection | None = None
    result: ResultSection | None = None
    validation: ValidationSection | None = None
    parity: ParitySection | None = None
