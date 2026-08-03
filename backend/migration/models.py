"""Migration engine contract (plan, apply, data load)."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from backend.assessment.models import ColumnInfo, ProgrammableObject, SecretRef, TableInfo
from backend.schema_migration.naming import IdentifierCase


# --- Target connection -----------------------------------------------------------


class LakebaseConnRequest(BaseModel):
    host: str
    database: str
    user: str
    # May be empty on later requests — the routes then fall back to the password
    # cached from the last successful connection (see connectors/credentials.py),
    # mirroring the source-side ConnectionRequest contract.
    password: str = ""
    # Alternative to a typed password: resolve it from a Databricks / Key Vault-backed
    # secret at request time. Mirrors ConnectionRequest.secret_ref on the source side.
    secret_ref: SecretRef | None = None
    port: int = 5432
    sslmode: str = "require"
    # Scopes the remembered/resolved target password to one migration project.
    # Not a connection field — excluded when building a LakebaseConnection.
    project_id: str = ""

    def conn_kwargs(self) -> dict:
        """Fields for ``LakebaseConnection`` — everything but the credential scope
        and the secret reference (neither is a psycopg connection parameter)."""
        return self.model_dump(exclude={"project_id", "secret_ref"})


# --- Plan ------------------------------------------------------------------------


class ObjectKind(str, Enum):
    SCHEMA = "schema"
    TABLE = "table"
    FUNCTION = "function"
    VIEW = "view"
    PROCEDURE = "procedure"
    TRIGGER = "trigger"
    # Post-data kinds — created only AFTER the data load (bulk-load performance:
    # no per-row index maintenance / FK validation during COPY, and identity
    # sequences sync to MAX+1 once rows exist).
    CONSTRAINT = "constraint"    # PKs, column defaults, identity, checks
    INDEX = "index"
    FOREIGN_KEY = "foreign_key"


# Apply order — schema, then tables, then code objects by dependency likelihood,
# then the post-data phase: constraints → indexes → FKs (which need the
# referenced PKs/unique indexes) → triggers (moved after data so they don't
# fire during the bulk COPY).
KIND_ORDER: dict[ObjectKind, int] = {
    ObjectKind.SCHEMA: 0,
    ObjectKind.TABLE: 1,
    ObjectKind.FUNCTION: 2,
    ObjectKind.VIEW: 3,
    ObjectKind.PROCEDURE: 4,
    ObjectKind.CONSTRAINT: 5,
    ObjectKind.INDEX: 6,
    ObjectKind.FOREIGN_KEY: 7,
    ObjectKind.TRIGGER: 8,
}

# Kinds applied after the data load. Mirrored by the SPA (CreateSync splits the
# plan into the two phases around the data run) and by the async snapshot
# notebook, which executes these items itself after the copy finishes.
POST_DATA_KINDS: frozenset[ObjectKind] = frozenset(
    {ObjectKind.CONSTRAINT, ObjectKind.INDEX, ObjectKind.FOREIGN_KEY, ObjectKind.TRIGGER}
)


class PlanItem(BaseModel):
    id: str
    kind: ObjectKind
    name: str
    sql: str                 # editable target SQL applied to Lakebase
    original: str = ""       # source T-SQL (code objects), for side-by-side review
    reasoning: str = ""      # AI agent's reasoning for the translation (code objects)
    notes: str = ""


class BuildPlanRequest(BaseModel):
    tables: list[TableInfo] = []
    programmable_objects: list[ProgrammableObject] = []
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE
    translate: bool = True              # run AI translation for code objects
    endpoint: str | None = None         # FM endpoint override


class PlanResponse(BaseModel):
    items: list[PlanItem]


class PlanRunState(BaseModel):
    """Progress/result for a background plan build (polled by the UI).

    Plan generation translates every code object through a Foundation Model,
    which for a real schema runs well past the Databricks Apps ~120s request
    timeout — so it runs on a daemon thread and the UI polls this instead of
    holding one long request open.
    """
    run_id: str
    status: str = "running"              # running|success|failed
    objects_total: int = 0               # code objects being translated
    objects_done: int = 0
    items: list[PlanItem] | None = None  # set on success
    error: str | None = None


# --- Apply (schema + code) -------------------------------------------------------


class ItemStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ItemResult(BaseModel):
    id: str
    name: str
    kind: ObjectKind
    status: ItemStatus
    error: str | None = None
    duration_ms: int = 0


class ApplyRequest(BaseModel):
    lakebase: LakebaseConnRequest
    items: list[PlanItem]
    stop_on_error: bool = False


class ApplyResponse(BaseModel):
    results: list[ItemResult]
    success: int
    failed: int
    skipped: int


# --- Data load -------------------------------------------------------------------


class TableLoadSpec(BaseModel):
    schema_name: str
    table_name: str
    target_table: str | None = None         # defaults to table_name
    total_rows: int = 0                      # from assessment, for progress %
    columns: list[ColumnInfo] = []           # source types, for value coercion


class DataLoadRequest(BaseModel):
    # Source (same shape as the assessment connection).
    source_type: str = "azure-sql"
    host: str
    database: str
    username: str
    # Optional when secret_ref supplies the credential. Keep the same contract as
    # ConnectionRequest and LakebaseConnRequest so ref-only API clients do not
    # need to send a placeholder empty string.
    password: str = ""
    # Alternative to a typed source password: a Databricks / Key Vault-backed
    # secret reference (mirrors ConnectionRequest.secret_ref).
    secret_ref: SecretRef | None = None
    port: int = 1433

    lakebase: LakebaseConnRequest
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE
    tables: list[TableLoadSpec] = Field(..., min_length=1)

    truncate_first: bool = True
    batch_size: int = 5000
    # Scopes the remembered/resolved source password to one migration project.
    project_id: str = ""


class TableProgress(BaseModel):
    name: str
    target: str
    status: ItemStatus | str = "pending"     # pending|running|success|failed
    rows_copied: int = 0
    total_rows: int = 0
    error: str | None = None


class RunState(BaseModel):
    run_id: str
    status: str = "running"                   # running|success|failed|partial
    tables: list[TableProgress] = []
    error: str | None = None
