"""FastAPI entry point for Lakebase Express.

Responsibilities:
  * mount the migration REST API under /api/*
  * serve the compiled React SPA (frontend/dist) for all other routes, with
    history-API fallback so client-side routing works on refresh.
"""
from __future__ import annotations

import contextlib
import logging
import pathlib

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.assessment_routes import router as assessment_router
from backend.api.data_routes import router as data_router
from backend.api.databricks_routes import router as databricks_router
from backend.api.migration_routes import router as migration_router
from backend.api.projects_routes import router as projects_router
from backend.api.query_parity_routes import router as query_parity_router
from backend.api.schema_routes import router as schema_router
from backend.api.settings_routes import router as settings_router
from backend.api.sizing_routes import router as sizing_router
from backend.api.validation_routes import router as validation_router
from backend.egress import log_egress_ip

logging.basicConfig(level=logging.INFO)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # Opt-in (LBX_EGRESS_PROBE): log the app's public egress IP once so it's
    # visible in `databricks apps logs` for Azure SQL firewall allowlisting. A
    # no-op by default; when on it runs in a daemon thread, so it never blocks
    # startup or readiness checks.
    log_egress_ip()
    yield


app = FastAPI(title="Lakebase Express", version="0.1.0", lifespan=lifespan)

# --- API routers (one per migration phase) ---------------------------------------
app.include_router(assessment_router)
app.include_router(sizing_router)
app.include_router(schema_router)
app.include_router(data_router)
app.include_router(settings_router)
app.include_router(migration_router)
app.include_router(validation_router)
app.include_router(query_parity_router)
app.include_router(projects_router)
app.include_router(databricks_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- Static SPA ------------------------------------------------------------------
_DIST = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"

if _DIST.is_dir():
    # assets/* (hashed JS/CSS) served directly; everything else falls back to
    # index.html for client-side routing.
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        candidate = _DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
