"""REST endpoints for the Sizing & Cost phase."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.sizing.calculator import estimate
from backend.sizing.models import SizingRequest, SizingResult

router = APIRouter(prefix="/api/sizing", tags=["sizing"])


@router.post("/estimate", response_model=SizingResult)
def estimate_sizing(req: SizingRequest) -> SizingResult:
    try:
        return estimate(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
