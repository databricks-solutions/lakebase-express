"""REST endpoints for the post-migration validation module.

Start + poll for the comparison run; a synchronous AI fix proposal for a single
inconsistency. Manual/AI fixes are *applied* through the existing
/api/migration/apply endpoint, and data re-copies through /api/migration/data —
validation reuses the migration engine rather than duplicating it.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.api.migration_routes import with_lakebase_password
from backend.connectors.credentials import resolve_effective_password
from backend.connectors.factory import build_connector
from backend.validation import agent, runs
from backend.validation.fixer import propose_fix
from backend.validation.models import (
    FixProposal,
    FixProposalRequest,
    RepairState,
    ValidationRepairRequest,
    ValidationRunRequest,
    ValidationRunState,
)

log = logging.getLogger("lakebase_express.validation")

router = APIRouter(prefix="/api/validation", tags=["validation"])


class StartValidationResponse(BaseModel):
    run_id: str


@router.post("/start", response_model=StartValidationResponse)
def start(req: ValidationRunRequest) -> StartValidationResponse:
    """Launch a background run that scans both sides and diffs them."""
    # Same session fallback as the assessment: the SPA never persists passwords,
    # so a reload sends an empty one.
    src = req.source
    password, src_ref = resolve_effective_password(
        src.source_type, src.host, src.database, src.username, src.project_id,
        src.password, src.secret_ref,
    )
    if not password:
        raise HTTPException(
            status_code=400,
            detail="No source password supplied and none cached from a previous successful "
                   "connection — re-enter it on Connections & Target.",
        )
    try:  # validate the source type before spawning the thread
        build_connector(src.source_type, host=src.host, database=src.database,
                        username=src.username, password=password, port=src.port)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    req = req.model_copy(update={
        # Stamp the effective secret_ref back so the run persists the pointer, not
        # the resolved value (mirrors with_lakebase_password on the target side).
        "source": src.model_copy(update={"password": password, "secret_ref": src_ref}),
        # Same fallback for the target side (shared with the migration routes).
        "lakebase": with_lakebase_password(req.lakebase),
    })
    return StartValidationResponse(run_id=runs.start_run(req))


@router.get("/status/{run_id}", response_model=ValidationRunState)
def status(run_id: str) -> ValidationRunState:
    state = runs.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown validation run id.")
    return state


@router.post("/fix", response_model=FixProposal)
def fix(req: FixProposalRequest) -> FixProposal:
    """Ask the Foundation Model for remediation SQL for one inconsistency.
    Fail-soft: errors come back in the payload, never as a 5xx."""
    return propose_fix(req.item, req.target_schema, req.endpoint)


# --- AI repair agent (autonomous remediation) --------------------------------------


class StartRepairResponse(BaseModel):
    run_id: str


@router.post("/repair/start", response_model=StartRepairResponse)
def start_repair(req: ValidationRepairRequest) -> StartRepairResponse:
    """Launch the agent loop that generates a fix for each open inconsistency,
    applies it to Lakebase, and iterates on any Postgres error until done."""
    # An empty password resolves from the session cache (clear 400 otherwise).
    req = req.model_copy(update={"lakebase": with_lakebase_password(req.lakebase)})
    return StartRepairResponse(run_id=agent.start_repair(req))


@router.get("/repair/status/{run_id}", response_model=RepairState)
def repair_status(run_id: str) -> RepairState:
    state = agent.get_repair(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown repair run id.")
    return state
