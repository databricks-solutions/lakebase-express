"""Workspace settings endpoints (Foundation Model discovery)."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config import FM_API, FM_API_GATEWAY, FM_ENDPOINT, workspace_client

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
    # Which API carries the chat calls ("serving" | "gateway"), so the UI can say
    # which naming form applies. Set by LBX_FM_API at startup.
    api: str


# Heuristic: chat-capable endpoints used for translation.
_CHAT_HINTS = ("chat", "instruct", "llama", "claude", "mixtral", "gpt", "gemma", "qwen", "dbrx")


def _looks_chat(name: str, task: str | None) -> bool:
    if task and "chat" in task.lower():
        return True
    return any(h in name.lower() for h in _CHAT_HINTS)


def _gateway_id(name: str) -> str:
    """The AI Gateway model id for a pay-per-token endpoint name.

    The console lists these as ``system.ai.<model>``, dropping the endpoint's
    ``databricks-`` prefix (``databricks-claude-opus-4-8`` ->
    ``system.ai.claude-opus-4-8``).
    """
    return f"system.ai.{name.removeprefix('databricks-')}"


def _with_gateway_ids(items: list[FmEndpoint]) -> list[FmEndpoint]:
    """Add the ``system.ai.*`` aliases the gateway route also accepts.

    Only for ``databricks-`` prefixed (pay-per-token) endpoints: custom and
    external-model endpoints have no ``system.ai`` id. Both forms hit the same
    model, so the alias is offered alongside the endpoint name rather than
    replacing it.
    """
    aliases = [
        item.model_copy(update={"name": _gateway_id(item.name)})
        for item in items
        if item.name.startswith("databricks-")
    ]
    return sorted([*items, *aliases], key=lambda x: x.name)


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
        if FM_API == FM_API_GATEWAY:
            items = _with_gateway_ids(items)
        return FmEndpointList(default=FM_ENDPOINT, endpoints=items, api=FM_API)
    except Exception as exc:
        log.warning("Could not list serving endpoints: %s", exc)
        return FmEndpointList(default=FM_ENDPOINT, endpoints=[], error=str(exc), api=FM_API)
