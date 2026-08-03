"""Migration project CRUD."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.connectors.credentials import clear_project as clear_project_credentials
from backend.projects.models import Project, ProjectSummary, SourceConfig
from backend.projects.store import get_store

router = APIRouter(prefix="/api/projects", tags=["projects"])
log = logging.getLogger("lakebase_express.projects")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CreateProjectRequest(BaseModel):
    name: str
    source_connector_id: str = "azure-sql"


@router.get("", response_model=list[ProjectSummary])
def list_projects() -> list[ProjectSummary]:
    return get_store().list()


@router.post("", response_model=Project)
def create_project(req: CreateProjectRequest) -> Project:
    now = _now()
    project = Project(
        id=str(uuid.uuid4()),
        name=req.name.strip() or "Untitled migration",
        source_connector_id=req.source_connector_id,
        created_at=now,
        updated_at=now,
        source=SourceConfig(source_type=req.source_connector_id),
    )
    get_store().save(project)
    return project


@router.get("/{project_id}", response_model=Project)
def get_project(project_id: str) -> Project:
    project = get_store().get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


@router.put("/{project_id}", response_model=Project)
def update_project(project_id: str, project: Project) -> Project:
    store = get_store()
    existing = store.get(project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found.")
    if project.identifier_case != existing.identifier_case:
        # Plans embed mapped names in every SQL statement, and validation embeds
        # them in its comparison results. Never retain artifacts from the old
        # policy when clients change casing through the API directly.
        project.plan = None
        project.validation = None
    project.id = project_id  # path is authoritative
    project.updated_at = _now()
    store.save(project)
    return project


@router.delete("/{project_id}")
def delete_project(project_id: str) -> dict:
    # Clean credentials first. If that fails, retain the project so the user can
    # retry instead of silently orphaning encrypted passwords/secret references.
    try:
        clear_project_credentials(project_id)
    except Exception as exc:
        log.exception("Could not delete credentials for project %s", project_id)
        raise HTTPException(
            status_code=502,
            detail="Project was not deleted because its stored credentials could not be removed.",
        ) from exc
    get_store().delete(project_id)
    return {"ok": True}
