"""Migration report export — the audit deliverable for a project.

Two views of one model: ``report`` is the self-contained HTML page (printed to PDF
from the browser), ``report-data`` the JSON behind it. ``scope=assessment`` narrows
either to the source scan alone, which is the deliverable while the rest of the
cycle has not happened yet.

Shares the ``/api/projects`` prefix with the project CRUD routes; the paths never
collide because these carry a second segment.
"""
from __future__ import annotations

from enum import Enum

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from backend import __version__
from backend.projects.models import Project
from backend.projects.store import get_store
from backend.report.builder import build_report
from backend.report.html import render_report
from backend.report.models import SCOPE_ASSESSMENT, SCOPE_FULL, MigrationReport

router = APIRouter(prefix="/api/projects", tags=["report"])


class ReportScope(str, Enum):
    """An enum, so a misspelled scope is a 422 rather than a silent full export."""

    FULL = SCOPE_FULL
    ASSESSMENT = SCOPE_ASSESSMENT


def _project(project_id: str) -> Project:
    project = get_store().get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _report(project_id: str, scope: ReportScope) -> MigrationReport:
    return build_report(
        _project(project_id), tool_version=__version__, scope=scope.value
    )


@router.get("/{project_id}/report", response_class=HTMLResponse)
def report_html(
    project_id: str,
    scope: ReportScope = Query(ReportScope.FULL, description="full | assessment"),
) -> HTMLResponse:
    """The printable report — one HTML file with nothing external to fetch."""
    return HTMLResponse(
        render_report(_report(project_id, scope)), media_type="text/html; charset=utf-8"
    )


@router.get("/{project_id}/report-data", response_model=MigrationReport)
def report_data(
    project_id: str,
    scope: ReportScope = Query(ReportScope.FULL, description="full | assessment"),
) -> MigrationReport:
    """The same report as JSON, for machine consumers."""
    return _report(project_id, scope)
