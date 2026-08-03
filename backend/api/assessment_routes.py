"""REST endpoints for the Connection & Assessment phase."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from backend.assessment.models import (
    AssessmentReport,
    ConnectionRequest,
    ConnectionResult,
)
from backend.assessment.models import SecretRef
from backend.assessment.scanner import run_assessment
from backend.connectors.credentials import remember_effective, resolve_effective_password
from backend.connectors.factory import build_connector

log = logging.getLogger("lakebase_express.assessment")

router = APIRouter(prefix="/api/assessment", tags=["assessment"])


def _to_connection(req: ConnectionRequest) -> tuple[object, SecretRef | None]:
    # Resolve the password by precedence: typed → request secret_ref → stored
    # secret_ref → cached plaintext. The SPA holds passwords per browser session,
    # so a reload sends an empty one; the fallbacks avoid a confusing driver error.
    # Returns the connector plus the secret reference that produced the password
    # (None when it was typed/cached), so a successful connect persists the
    # pointer rather than the value.
    password, ref = resolve_effective_password(
        req.source_type, req.host, req.database, req.username, req.project_id,
        req.password, req.secret_ref,
    )
    if not password:
        raise HTTPException(
            status_code=400,
            detail="No password supplied and none cached from a previous successful "
                   "connection — re-enter it on Connections & Target.",
        )
    try:
        return build_connector(
            req.source_type,
            host=req.host,
            database=req.database,
            username=req.username,
            password=password,
            port=req.port,
        ), ref
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _remember(req: ConnectionRequest, conn, ref: SecretRef | None) -> None:
    remember_effective(req.source_type, req.host, req.database, req.username, req.project_id,
                       conn.password, ref)


@router.post("/test-connection", response_model=ConnectionResult)
def test_connection(req: ConnectionRequest) -> ConnectionResult:
    try:
        conn, ref = _to_connection(req)
        ok = conn.test_connection()
        if ok:
            _remember(req, conn, ref)
        return ConnectionResult(ok=ok, message="Connection successful." if ok else "Probe failed.")
    except HTTPException:
        raise
    except Exception as exc:  # surface a clean message; full trace stays in logs
        log.exception("Connection test failed")
        return ConnectionResult(ok=False, message=str(exc))


@router.post("/scan", response_model=AssessmentReport)
def scan(req: ConnectionRequest, endpoint: str | None = None, use_ai: bool = True) -> AssessmentReport:
    """Scan the source and (by default) run the AI migration analysis.

    ``endpoint`` (query param) overrides the Foundation Model serving endpoint;
    ``use_ai=false`` runs only the deterministic scan.
    """
    conn, ref = _to_connection(req)
    try:
        report = run_assessment(conn, use_ai=use_ai, endpoint=endpoint)
    except Exception as exc:
        log.exception("Assessment scan failed")
        raise HTTPException(status_code=502, detail=f"Scan failed: {exc}") from exc
    _remember(req, conn, ref)  # the scan authenticated — keep the credential for the session
    return report
