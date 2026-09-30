"""App-migration context export — the artifact handed to a downstream agent.

Two views of the same model: ``context-skill`` is the drop-in ``SKILL.md`` people
actually hand over, ``context-bundle`` is the JSON behind it for machine consumers.

Both are derived deterministically from stored state. The one exception is the
model notes over the translated objects: those are produced by an explicit
background run (``context-notes``), and once a run has succeeded the exports carry
its result as a clearly-labelled advisory section. Nothing here ever calls a model
inline — it takes minutes, past the Apps request timeout.

Shares the ``/api/projects`` prefix with the project CRUD routes; the two never
collide because these paths have a second segment.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from backend import __version__
from backend.context_bundle import runs
from backend.context_bundle.builder import build_bundle
from backend.context_bundle.models import AiNotesRunState, ContextBundle
from backend.context_bundle.skill import render_skill
from backend.projects.models import Project
from backend.projects.store import get_store

router = APIRouter(prefix="/api/projects", tags=["context"])


class StartNotesResponse(BaseModel):
    run_id: str


def _project(project_id: str) -> Project:
    project = get_store().get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _bundle(project_id: str) -> ContextBundle:
    """The bundle, carrying the newest successful notes run if there is one."""
    return build_bundle(
        _project(project_id),
        tool_version=__version__,
        ai_notes=runs.latest_notes(project_id),
    )


@router.get("/{project_id}/context-skill", response_class=PlainTextResponse)
def context_skill(project_id: str) -> PlainTextResponse:
    """The project's ``SKILL.md`` — drop it into another agent's skills directory."""
    return PlainTextResponse(
        render_skill(_bundle(project_id)), media_type="text/markdown; charset=utf-8"
    )


@router.get("/{project_id}/context-bundle", response_model=ContextBundle)
def context_bundle(project_id: str) -> ContextBundle:
    """The same context as JSON, for machine consumers."""
    return _bundle(project_id)


@router.post("/{project_id}/context-notes", response_model=StartNotesResponse)
def start_notes(project_id: str, endpoint: str | None = None) -> StartNotesResponse:
    """Start the model pass over the translated objects; poll the run for its result.

    Not done inline: reading every object takes minutes, past the Databricks Apps
    ~120s request timeout.
    """
    return StartNotesResponse(run_id=runs.start_run(_project(project_id), endpoint))


@router.get("/{project_id}/context-notes/status/{run_id}", response_model=AiNotesRunState)
def notes_status(project_id: str, run_id: str) -> AiNotesRunState:
    state = runs.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown notes run id.")
    return state
