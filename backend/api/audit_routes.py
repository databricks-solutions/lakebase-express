"""Audit trail — who ran which migration step and touched which credential."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend import audit

router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditEntry(BaseModel):
    at: str
    actor: str
    action: str           # run.started | run.finished | schema.applied | credential.read | …
    project_id: str | None = None
    target: str = ""
    outcome: str = "ok"
    detail: dict = {}


class AuditTrail(BaseModel):
    # False when the store is process memory, i.e. the trail dies with the process.
    persistent: bool
    events: list[AuditEntry]


@router.get("", response_model=AuditTrail)
def trail(
    project_id: str | None = None,
    action: str | None = Query(default=None, description="Exact action, or a prefix ending in '.'"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> AuditTrail:
    store = audit.get_audit_store()
    return AuditTrail(
        persistent=not isinstance(store, audit.MemoryAuditStore),
        events=[AuditEntry(**asdict(e)) for e in store.list(project_id, action, limit)],
    )
