"""REST endpoints for the post-migration query-parity module.

Two steps, mirroring how the rest of the app splits AI generation from execution:

  * ``/generate`` — one synchronous Foundation Model call turns the migrated
    schema into ``count`` synthetic read-only query pairs (T-SQL + PostgreSQL).
    The user reviews/edits them before running. Fail-soft: AI errors come back in
    the payload, never as a 5xx.
  * ``/run/start`` + ``/run/status`` — a background run executes every pair
    against the source and Lakebase, compares the results, and reports parity.
    Same start/poll shape as the validation run.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.api.migration_routes import with_lakebase_password
from backend.connectors.credentials import resolve_effective_password
from backend.connectors.factory import build_connector
from backend.query_parity import runner
from backend.query_parity.generator import generate_queries
from backend.query_parity.models import (
    GenerateQueriesRequest,
    GenerateQueriesResponse,
    QueryParityRunRequest,
    QueryParityRunState,
)

log = logging.getLogger("lakebase_express.query_parity")

router = APIRouter(prefix="/api/query-parity", tags=["query-parity"])


@router.post("/generate", response_model=GenerateQueriesResponse)
def generate(req: GenerateQueriesRequest) -> GenerateQueriesResponse:
    """Generate synthetic read-only query pairs over the migrated schema."""
    return generate_queries(
        req.tables, req.count, req.target_schema, req.identifier_case, req.endpoint
    )


class StartRunResponse(BaseModel):
    run_id: str


@router.post("/run/start", response_model=StartRunResponse)
def start(req: QueryParityRunRequest) -> StartRunResponse:
    """Launch a background run that executes each pair on both sides and diffs them."""
    # Same session-credential fallback as the validation run: the SPA never
    # persists passwords, so a reload sends them empty.
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
        "source": src.model_copy(update={"password": password, "secret_ref": src_ref}),
        "lakebase": with_lakebase_password(req.lakebase),
    })
    return StartRunResponse(run_id=runner.start_run(req))


@router.get("/run/status/{run_id}", response_model=QueryParityRunState)
def status(run_id: str) -> QueryParityRunState:
    state = runner.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown query-parity run id.")
    return state
