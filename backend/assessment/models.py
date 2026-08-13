"""Pydantic models — the shared contract between backend and React frontend.

These serialize directly to the JSON the SPA consumes, so field names are the
API surface. Keep them stable.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """How much manual effort a compatibility finding implies."""

    INFO = "info"  # auto-handled by the type/DDL mapper
    LOW = "low"  # mechanical change, low risk
    MEDIUM = "medium"  # needs review, likely scripted
    HIGH = "high"  # manual rewrite required (e.g. cursors, dynamic SQL)


# --- Connection ------------------------------------------------------------------


class SecretRef(BaseModel):
    """A pointer to a password held in a secret manager instead of typed inline.

    Resolution always goes through the *same* Databricks Secrets API and is
    cloud-agnostic: a scope is read by name whatever backs it. On Azure workspaces
    a scope can be **Key Vault-backed** (an Azure-native feature the user registers
    once), in which case ``key`` is the Key Vault secret name and the read passes
    through to the vault transparently; on AWS/GCP only Databricks-native scopes
    exist and resolution is identical. ``kind`` only drives UI labelling — the
    backend does not branch on it (see
    ``connectors/credentials.resolve_secret_ref``). The project/credential stores
    persist only this reference, never the resolved value. Async job setup can
    explicitly copy the value into a user-selected Databricks runtime scope.
    """

    kind: str = Field("databricks", description='"databricks" or "azure_key_vault" (UI label only)')
    # Scope/key names are only meaningful inside one workspace. New references
    # carry that workspace so switching OAuth sessions cannot silently resolve a
    # same-named secret somewhere else. Optional for references saved before this
    # field existed; they continue to resolve with the legacy current-workspace
    # behaviour until the user re-selects them.
    workspace_host: str | None = Field(
        None,
        description="Databricks workspace host this scope/key belongs to.",
    )
    scope: str = Field(..., description="Databricks secret scope name (any backend, incl. KV-backed on Azure).")
    key: str = Field(..., description="Secret key within the scope (the Key Vault secret name for a KV-backed scope).")


class ConnectionRequest(BaseModel):
    # Catalog connector id, e.g. "azure-sql" or "sql-server".
    source_type: str = "azure-sql"
    host: str = Field(..., examples=["myserver.database.windows.net"])
    database: str
    username: str
    # Entered directly in the UI and held in memory only for the scan. May be
    # empty on later requests — the backend then falls back to the password
    # cached from the last successful connection (see connectors/credentials.py).
    # The generated migration notebooks (Data tab) use a Databricks secret scope
    # instead, since plaintext passwords must never be embedded in notebooks.
    password: str = Field("", description="Azure SQL password; may be empty if a previous connection succeeded this session.")
    # Alternative to a typed password: resolve it from a secret manager at request
    # time. When set, it takes precedence over any cached plaintext and is what
    # gets persisted for this connection (see connectors/credentials.py).
    secret_ref: SecretRef | None = None
    port: int = 1433
    # Scopes a remembered/resolved password to one migration project. Empty for
    # project-agnostic callers (keeps the legacy shared-cache behaviour).
    project_id: str = ""


class ConnectionResult(BaseModel):
    ok: bool
    message: str


# --- Scan results ----------------------------------------------------------------


class ColumnInfo(BaseModel):
    name: str
    data_type: str  # SQL Server type, e.g. "nvarchar"
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_nullable: bool = True
    # Collation of a character column, e.g. "SQL_Latin1_General_CP1_CI_AS" (None for
    # other types). Drives the target's COLLATE clause — Postgres defaults to
    # case-SENSITIVE, so dropping it silently changes equality, ORDER BY, GROUP BY
    # and unique-index results (schema_migration/collation_mapper). Optional so
    # reports saved before collations were scanned still load.
    collation_name: str | None = None


class ForeignKeyInfo(BaseModel):
    """One foreign key on a table (column lists are in constraint order)."""

    name: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]
    # sys.foreign_keys action descriptions: NO_ACTION | CASCADE | SET_NULL | SET_DEFAULT.
    on_delete: str = "NO_ACTION"
    on_update: str = "NO_ACTION"


class IndexColumnInfo(BaseModel):
    name: str
    descending: bool = False


class IndexInfo(BaseModel):
    """A source index (unique constraints are scanned as unique indexes — a
    unique index satisfies FK references in Postgres just like a constraint)."""

    name: str
    columns: list[IndexColumnInfo]
    include_columns: list[str] = []
    is_unique: bool = False
    # T-SQL predicate of a filtered index (mechanically translated for Postgres).
    filter_definition: str | None = None


class ColumnDefaultInfo(BaseModel):
    column: str
    definition: str  # raw T-SQL default expression, e.g. "(getdate())"


class CheckConstraintInfo(BaseModel):
    name: str
    definition: str  # raw T-SQL predicate, e.g. "([Qty]>(0))"


class TableInfo(BaseModel):
    schema_name: str
    table_name: str
    row_count: int
    column_count: int
    columns: list[ColumnInfo] = []
    # Primary-key column(s), in key order (empty if the table has no PK).
    # Assessment metadata surfaced in the UI; a PK also enables parallel/
    # partitioned reads. Populated by the scanner; defaults empty for compatibility.
    primary_key: list[str] = []
    # Constraint/index metadata for the post-data phase of the migration plan
    # (created only after the data load, for bulk-load performance). All default
    # empty so reports saved before this scan existed still load.
    identity_column: str | None = None
    foreign_keys: list[ForeignKeyInfo] = []
    indexes: list[IndexInfo] = []
    column_defaults: list[ColumnDefaultInfo] = []
    check_constraints: list[CheckConstraintInfo] = []

    @property
    def fqn(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


class ProgrammableObject(BaseModel):
    """A stored procedure, view, function, or trigger from the source."""

    schema_name: str
    object_name: str
    object_type: str  # PROCEDURE | VIEW | FUNCTION | TRIGGER
    line_count: int
    definition: str  # the T-SQL body (used by the rule engine + later AI translation)


# --- Compatibility findings ------------------------------------------------------


class Finding(BaseModel):
    rule_id: str
    title: str
    severity: Severity
    object_name: str  # where it was found (table fqn or programmable object)
    detail: str
    recommendation: str


# --- AI migration analysis -------------------------------------------------------
#
# Layered on TOP of the deterministic scan/rules: a Foundation Model reasons over
# the scanned schema, code, and rule findings to surface deeper semantic /
# behavioral / operational risks the regex rules can't see. Advisory, not
# authoritative — the deterministic readiness score stays the factual anchor.


class AIRisk(BaseModel):
    title: str
    category: str = ""            # schema | data-type | stored-logic | performance | operational | data-integrity
    severity: str = "medium"      # high | medium | low
    affected_objects: str = ""
    rationale: str = ""
    recommendation: str = ""


class AIAssessment(BaseModel):
    summary: str = ""
    complexity: str = "Medium"    # Low | Medium | High
    complexity_rationale: str = ""
    estimated_effort: str = ""
    risks: list[AIRisk] = []
    recommendations: list[str] = []
    endpoint: str = ""            # serving endpoint that produced this
    success: bool = False
    error: str | None = None


class AssessmentReport(BaseModel):
    database: str
    table_count: int
    total_rows: int
    programmable_object_count: int
    findings: list[Finding]

    # Headline readiness score 0–100 (100 = fully automatable).
    readiness_score: int
    severity_counts: dict[str, int]

    tables: list[TableInfo]
    programmable_objects: list[ProgrammableObject]

    # Optional AI deep-dive; None if AI analysis was skipped or unavailable.
    ai_assessment: AIAssessment | None = None
