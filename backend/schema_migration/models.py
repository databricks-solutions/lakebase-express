"""Schema & code migration contract."""
from __future__ import annotations

from pydantic import BaseModel

from backend.assessment.models import ProgrammableObject, TableInfo
from backend.schema_migration.naming import IdentifierCase


class DDLRequest(BaseModel):
    tables: list[TableInfo]
    target_schema: str = "public"  # Lakebase/Postgres schema to create into
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE


class DDLResult(BaseModel):
    ddl: str               # full runnable script
    statement_count: int


class TranslateRequest(BaseModel):
    objects: list[ProgrammableObject]
    target_schema: str = "public"
    identifier_case: IdentifierCase = IdentifierCase.LOWERCASE
    endpoint: str | None = None  # override Foundation Model serving endpoint


class Translation(BaseModel):
    object_name: str
    object_type: str
    original: str
    translated: str
    reasoning: str = ""    # how the AI agent decided the translation
    notes: str
    success: bool


class TranslateResult(BaseModel):
    translations: list[Translation]
