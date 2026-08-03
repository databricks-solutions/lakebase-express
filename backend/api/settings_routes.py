"""Workspace settings endpoints (Foundation Model discovery)."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config import FM_ENDPOINT, workspace_client

log = logging.getLogger("lakebase_express.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])


class FmEndpoint(BaseModel):
    name: str
    task: str | None = None
    ready: bool = True


class FmEndpointList(BaseModel):
    default: str
    endpoints: list[FmEndpoint]
    error: str | None = None


# Heuristic: chat-capable endpoints used for translation.
_CHAT_HINTS = ("chat", "instruct", "llama", "claude", "mixtral", "gpt", "gemma", "qwen", "dbrx")


def _looks_chat(name: str, task: str | None) -> bool:
    if task and "chat" in task.lower():
        return True
    return any(h in name.lower() for h in _CHAT_HINTS)


@router.get("/fm-endpoints", response_model=FmEndpointList)
def fm_endpoints() -> FmEndpointList:
    """List serving endpoints likely usable for T-SQL translation.

    Falls back gracefully (empty list + error message) if the app identity lacks
    permission to list serving endpoints — the Schema phase still works with the
    configured default endpoint.
    """
    try:
        items: list[FmEndpoint] = []
        for e in workspace_client().serving_endpoints.list():
            task = getattr(e, "task", None)
            if not _looks_chat(e.name or "", task):
                continue
            state = getattr(getattr(e, "state", None), "ready", None)
            items.append(
                FmEndpoint(name=e.name, task=task, ready=str(state).upper() != "NOT_READY")
            )
        items.sort(key=lambda x: x.name)
        return FmEndpointList(default=FM_ENDPOINT, endpoints=items)
    except Exception as exc:
        log.warning("Could not list serving endpoints: %s", exc)
        return FmEndpointList(default=FM_ENDPOINT, endpoints=[], error=str(exc))
