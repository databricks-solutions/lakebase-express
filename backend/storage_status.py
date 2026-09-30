"""Where the app persists its state, and whether that survives a restart.

Every store falls back rather than fail: projects to the container's filesystem,
credentials and run state to process memory. A Databricks App gets a fresh container
on each restart, so a deployment that never received ``LBX_PROJECTS_BACKEND=postgres``
silently loses its projects — and the run state a resume needs — with no error
anywhere. This names what is actually in use, for the startup log and the UI.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("lakebase_express.storage")

# Store class -> (label, survives a restart).
_BACKENDS: dict[str, tuple[str, bool]] = {
    "PostgresStore": ("lakebase", True),
    "PostgresCredentialStore": ("lakebase", True),
    "PostgresRunStore": ("lakebase", True),
    "VolumeStore": ("uc-volume", True),
    "LocalFileStore": ("local-files", False),
    "MemoryCredentialStore": ("memory", False),
    "MemoryRunStore": ("memory", False),
}


@dataclass(frozen=True)
class StoreStatus:
    store: str            # projects | credentials | runs
    backend: str          # lakebase | uc-volume | local-files | memory | unavailable
    durable: bool         # survives an app restart
    detail: str = ""      # where it writes, or why it is unavailable


def _pg_detail(table_env: str, default_table: str) -> str:
    host = os.getenv("LBX_PROJECTS_PG_HOST", "?")
    database = os.getenv("LBX_PROJECTS_PG_DATABASE", "databricks_postgres")
    return f"{host}/{database}, table {os.getenv(table_env, default_table)}"


def _describe(store: str, factory: Callable[[], object], table_env: str, table: str) -> StoreStatus:
    try:
        instance = factory()
    except Exception as exc:
        # Only the project store raises; the other two fall back to memory themselves.
        return StoreStatus(store=store, backend="unavailable", durable=False, detail=str(exc))
    cls = type(instance).__name__
    backend, durable = _BACKENDS.get(cls, (cls, False))
    if backend == "lakebase":
        detail = _pg_detail(table_env, table)
    elif backend == "memory":
        detail = "this process only"
    else:
        detail = os.getenv("LBX_PROJECTS_DIR", "~/.lakebase-express/projects")
    return StoreStatus(store=store, backend=backend, durable=durable, detail=detail)


def storage_status() -> list[StoreStatus]:
    from backend.connectors.credential_store import get_credential_store
    from backend.projects.store import get_store
    from backend.run_store import get_run_store

    return [
        _describe("projects", get_store, "LBX_PROJECTS_PG_TABLE", "lbx_projects"),
        _describe("credentials", get_credential_store, "LBX_CREDENTIALS_PG_TABLE", "lbx_credentials"),
        _describe("runs", get_run_store, "LBX_RUNS_PG_TABLE", "lbx_runs"),
    ]


def warning(stores: list[StoreStatus] | None = None) -> str | None:
    """One line on what the next restart will lose, or None when all durable.

    A store configured for Lakebase but unreachable is called out separately: telling
    someone to set LBX_PROJECTS_BACKEND they have already set sends them the wrong way.
    """
    stores = storage_status() if stores is None else stores
    unreachable = [s for s in stores if s.backend == "unavailable"]
    if unreachable:
        return "; ".join(
            f"The {s.store} store is configured for Lakebase but cannot be opened, so nothing "
            f"is persisted: {s.detail}"
            for s in unreachable
        )
    lost = [s.store for s in stores if not s.durable]
    if not lost:
        return None
    return (
        f"{', '.join(lost).capitalize()} are not stored outside this app, so they are lost when "
        "it restarts. That is expected for local development; deployed, it means the app never "
        "received LBX_PROJECTS_BACKEND=postgres (with LBX_PROJECTS_PG_HOST and "
        "LBX_PROJECTS_PG_USER) — check the app's environment variables."
    )


def log_storage() -> None:
    """Say at startup what is being written where — a misconfigured deployment is
    otherwise indistinguishable from an empty one until data goes missing."""
    stores = storage_status()
    log.info(
        "Storage: %s", ", ".join(f"{s.store}={s.backend} ({s.detail})" for s in stores)
    )
    note = warning(stores)
    if note:
        log.warning("%s", note)
