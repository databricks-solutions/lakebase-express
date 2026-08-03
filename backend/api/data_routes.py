"""REST endpoints for the Data Migration (ETL generation) phase."""
from __future__ import annotations

from fastapi import APIRouter

from backend.data_migration.etl_generator import generate
from backend.data_migration.models import DataGenRequest, DataGenResult

router = APIRouter(prefix="/api/data", tags=["data"])


@router.post("/generate", response_model=DataGenResult)
def generate_etl(req: DataGenRequest) -> DataGenResult:
    return DataGenResult(artifacts=generate(req))
