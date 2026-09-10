"""Data migration (ETL generation) contract."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from backend.schema_migration.naming import IdentifierCase


class LoadMode(str, Enum):
    SNAPSHOT = "snapshot"   # one-off full copy of each table (source -> Lakebase)


class TableRef(BaseModel):
    schema_name: str
    table_name: str
    # Primary-key column(s), in key order; auto-filled from the assessment scan.
    # Not required for the snapshot copy — kept for parity with the assessment model.
    primary_key: list[str] = []


class PostLoadStatement(BaseModel):
    """One post-data plan item the snapshot job applies itself after the copy
    (the job may run detached from the app). ``kind`` mirrors the plan's
    ObjectKind (constraint / index / foreign_key / trigger) so async mode can
    group the statements into one job task per type."""

    name: str
    sql: str
    kind: str = "constraint"


class RunStoreTarget(BaseModel):
    """Lakebase table the job writes its run state to.

    No password: the job mints a short-lived OAuth credential from its own
    workspace identity, so nothing secret is embedded in the notebook.
    """
    host: str
    database: str
    table: str = "lbx_runs"
    port: int = 5432
    # Endpoint resource path the OAuth credential is minted for.
    endpoint: str


class DataGenRequest(BaseModel):
    mode: LoadMode = LoadMode.SNAPSHOT
    project_id: str = ""                 # links the async run to its project

    # Source (password resolved at runtime from the secret scope by the code).
    host: str
    database: str
    username: str
    password_secret_key: str
    secret_scope: str = "lakebase-express"
    port: int = 1433

    tables: list[TableRef] = Field(..., min_length=1)

    # Target: Lakebase (Postgres). The snapshot writes straight into the tables the
    # schema & code migration plan already created (the same plan Sync mode applies),
    # so it reuses the project's target schema.
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE
    lakebase_host: str
    lakebase_port: int = 5432
    lakebase_database: str = "lakebase"
    lakebase_user: str
    # Lakebase role password, read from the secret scope at runtime (never embedded).
    lakebase_password_secret_key: str

    # Post-data phase of the migration plan (constraints, indexes, FKs,
    # triggers), embedded into the generated notebook and applied by the job
    # AFTER the snapshot finishes. Kept as (name, sql) so the user's plan edits
    # ride along verbatim.
    post_load_sql: list[PostLoadStatement] = []

    # Where the job records its own run state (backend/run_store.py). Filled in
    # server-side from the app's own config, never by the client. Empty means the
    # notebook is generated without run-state reporting.
    run_store: RunStoreTarget | None = None


class Artifact(BaseModel):
    name: str
    filename: str
    language: str          # "python" | "sql"
    description: str
    code: str


class DataGenResult(BaseModel):
    artifacts: list[Artifact]
