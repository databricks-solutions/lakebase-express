"""REST endpoints for the Schema & Code migration phase."""
from __future__ import annotations

from fastapi import APIRouter

from backend.schema_migration.ai_translator import translate_all
from backend.schema_migration.ddl_generator import generate_ddl
from backend.schema_migration.naming import map_schema
from backend.schema_migration.models import (
    DDLRequest,
    DDLResult,
    TranslateRequest,
    TranslateResult,
)

router = APIRouter(prefix="/api/schema", tags=["schema"])


@router.post("/ddl", response_model=DDLResult)
def ddl(req: DDLRequest) -> DDLResult:
    script, count = generate_ddl(req.tables, req.target_schema, req.identifier_case)
    return DDLResult(ddl=script, statement_count=count)


@router.post("/translate", response_model=TranslateResult)
def translate(req: TranslateRequest) -> TranslateResult:
    schema_map = {
        o.schema_name: map_schema(o.schema_name, req.target_schema, req.identifier_case)
        for o in req.objects
    }
    return TranslateResult(translations=translate_all(
        req.objects,
        req.endpoint,
        schema_map=schema_map,
        identifier_case=req.identifier_case,
    ))
