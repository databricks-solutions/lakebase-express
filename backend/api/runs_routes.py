"""Run history — recent runs of every kind from the run store."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.run_store import MemoryRunStore, get_run_store

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunHistoryEntry(BaseModel):
    # sync_run | async_job | async_run | plan_build | validation | validation_repair
    # | query_parity
    kind: str
    run_id: str
    status: str
    updated_at: str
    project_id: str | None = None   # the lbx_projects row this run belongs to


class RunHistory(BaseModel):
    # False when the store is process memory, i.e. history dies with the process.
    persistent: bool
    runs: list[RunHistoryEntry]


@router.get("", response_model=RunHistory)
def history(
    kind: str | None = None,
    project_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> RunHistory:
    store = get_run_store()
    return RunHistory(
        persistent=not isinstance(store, MemoryRunStore),
        runs=[RunHistoryEntry(**asdict(r)) for r in store.list(kind, limit, project_id)],
    )
