"""Migration project model — the persistent unit of work.

A project bundles everything needed to start, leave, and resume a migration:
source/target connection *config* (never passwords), object selection, the
assessment, the plan, phase statuses, and run history. Secret *values* are never
stored here; non-secret scope/key/workspace references may be persisted.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from backend.assessment.models import SecretRef
from backend.schema_migration.naming import IdentifierCase


class PhaseStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class SourceConfig(BaseModel):
    source_type: str = "azure-sql"
    host: str = ""
    database: str = ""
    username: str = ""
    port: int = 1433
    # A secret-manager pointer (scope/key) chosen for the password. This is NOT a
    # secret — only the reference — so it is safe to persist and lets the resumed
    # session restore Secret mode instead of resetting to a typed password.
    secret_ref: SecretRef | None = None


class TargetConfig(BaseModel):
    host: str = ""
    database: str = "databricks_postgres"
    user: str = ""
    port: int = 5432
    sslmode: str = "require"
    # See SourceConfig.secret_ref — the non-secret password pointer for the target.
    secret_ref: SecretRef | None = None


class DataOptions(BaseModel):
    """Data-load options marked in the Data Migration step; consumed when the
    migration is concluded (run or scheduled) in the Create Sync step."""
    truncate_first: bool = True
    batch_size: int = 5000


class RunSummary(BaseModel):
    run_id: str
    kind: str = "data"            # data | schema
    status: str = "running"
    tables_total: int = 0
    tables_ok: int = 0
    rows_copied: int = 0
    finished_at: str | None = None


class Project(BaseModel):
    id: str
    name: str
    source_connector_id: str = "azure-sql"   # catalog tile id
    created_at: str
    updated_at: str

    source: SourceConfig = Field(default_factory=SourceConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE

    # Persisted artifacts (stored as plain JSON to stay decoupled from phase models).
    assessment: dict[str, Any] | None = None
    plan: list[dict[str, Any]] | None = None
    selection: list[str] = []                # selected "schema.table" keys (Data Migration)
    data_options: DataOptions = Field(default_factory=DataOptions)
    validation: dict[str, Any] | None = None  # latest post-migration ValidationReport
    query_parity: dict[str, Any] | None = None  # latest post-migration QueryParityReport

    statuses: dict[str, PhaseStatus] = Field(
        default_factory=lambda: {
            "assessment": PhaseStatus.NOT_STARTED,
            "sizing": PhaseStatus.NOT_STARTED,
            "schema": PhaseStatus.NOT_STARTED,
            "data": PhaseStatus.NOT_STARTED,
            "validation": PhaseStatus.NOT_STARTED,
        }
    )
    runs: list[RunSummary] = []


class ProjectSummary(BaseModel):
    id: str
    name: str
    source_connector_id: str
    target_host: str
    updated_at: str
    statuses: dict[str, PhaseStatus]


def to_summary(p: Project) -> ProjectSummary:
    return ProjectSummary(
        id=p.id,
        name=p.name,
        source_connector_id=p.source_connector_id,
        target_host=p.target.host,
        updated_at=p.updated_at,
        statuses=p.statuses,
    )
