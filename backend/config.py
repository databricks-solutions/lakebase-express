"""Central configuration and Databricks client wiring.

The app talks to Databricks for the Foundation Model calls (AI assessment + T-SQL
translation), serving-endpoint listing, and Lakeflow Job creation.

Auth is the **ambient identity** only, resolved once through the standard
Databricks SDK auth chain: running locally that is a CLI profile
(``DATABRICKS_CONFIG_PROFILE``) or ``DATABRICKS_HOST``/token; deployed as a
Databricks App it is the App's injected service-principal OAuth.

This deliberately binds the process to exactly one workspace — the one the
profile points at, or the one the App is published in — so a migration cannot be
pointed at a second workspace mid-session. There is no in-app workspace login;
to target a different workspace, restart with a different profile.
"""
from __future__ import annotations

import functools
import logging
import os

from databricks.sdk import WorkspaceClient

log = logging.getLogger("lakebase_express.config")

# --- Environment-driven settings -------------------------------------------------

# Secret scope that holds Azure SQL credentials (see app.yaml).
SECRET_SCOPE = os.getenv("LBX_SECRET_SCOPE", "lakebase-express")

# Foundation Model serving endpoint for the schema-translation phase.
FM_ENDPOINT = os.getenv("LBX_FM_ENDPOINT", "databricks-claude-opus-4-8")


# --- The single bound workspace ---------------------------------------------------


def _host_label(host: str) -> str:
    return (host or "").removeprefix("https://").removeprefix("http://").rstrip("/")


@functools.lru_cache(maxsize=1)
def workspace_client() -> WorkspaceClient:
    """The WorkspaceClient for the bound workspace.

    Cached: the SDK refreshes its own credentials, and the target never changes
    for the life of the process.
    """
    return WorkspaceClient()


@functools.lru_cache(maxsize=1)
def current_workspace() -> dict:
    """Status for the UI: which workspace we're bound to (cached; it can't change).

    Never raises — a misconfigured profile surfaces as ``connected: False`` with
    the reason, so the UI can tell the user what to fix instead of erroring out.
    """
    try:
        w = workspace_client()
        me = w.current_user.me()
        return {
            "connected": True,
            "host": _host_label(w.config.host or ""),
            "user": me.user_name,
        }
    except Exception as exc:  # no usable profile / injected identity
        log.info("No Databricks identity available: %s", exc)
        return {"connected": False, "error": str(exc)}
