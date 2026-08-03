"""Central configuration and Databricks client wiring.

The app talks to Databricks for the Foundation Model calls (AI assessment + T-SQL
translation), serving-endpoint listing, and Lakeflow Job creation. Two auth paths:

  * **User OAuth login** — the user signs in to a workspace from the UI (U2M
    OAuth). The resulting credentials live only in process memory for the
    session and are used for every Databricks call.
  * **Ambient identity** — when no user is logged in, the standard Databricks SDK
    auth chain is used (``DATABRICKS_HOST``/token, a CLI profile, or the OAuth
    identity injected when running as a Databricks App).
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

# OAuth (U2M) client used for workspace login. Defaults to the public Databricks
# CLI client; override with a custom OAuth app's id/secret for a deployed app.
OAUTH_CLIENT_ID = os.getenv("LBX_OAUTH_CLIENT_ID", "databricks-cli")
OAUTH_CLIENT_SECRET = os.getenv("LBX_OAUTH_CLIENT_SECRET") or None
OAUTH_SCOPES = ["all-apis", "offline_access"]


# --- Workspace session (user OAuth login) ---------------------------------------

# Single-user accelerator: the active login lives in module memory. Holds the
# workspace host, the SDK SessionCredentials (auto-refreshing), and the username.
_session: dict | None = None


def set_workspace_session(host: str, creds, user: str) -> None:
    global _session
    _session = {"host": host, "creds": creds, "user": user}


def clear_workspace_session() -> None:
    global _session
    _session = None


def has_oauth_session() -> bool:
    return _session is not None


def _host_label(host: str) -> str:
    return (host or "").removeprefix("https://").removeprefix("http://").rstrip("/")


@functools.lru_cache(maxsize=1)
def _ambient_client() -> WorkspaceClient:
    return WorkspaceClient()


@functools.lru_cache(maxsize=1)
def _ambient_status() -> dict:
    """Best-effort check of the ambient identity (cached; it doesn't change)."""
    try:
        w = _ambient_client()
        me = w.current_user.me()
        return {"connected": True, "source": "ambient", "host": _host_label(w.config.host or ""), "user": me.user_name}
    except Exception as exc:  # no ambient creds configured
        log.info("No ambient Databricks identity: %s", exc)
        return {"connected": False, "source": "ambient"}


def workspace_client() -> WorkspaceClient:
    """Return a WorkspaceClient for the active login, else the ambient identity.

    Built fresh per call when an OAuth session is active so the access token is
    refreshed by the SessionCredentials as needed.
    """
    if _session:
        token = _session["creds"].token().access_token
        return WorkspaceClient(host=_session["host"], token=token)
    return _ambient_client()


def current_workspace() -> dict:
    """Status for the UI: who/where we're connected to (or not)."""
    if _session:
        return {"connected": True, "source": "oauth", "host": _host_label(_session["host"]), "user": _session["user"]}
    return _ambient_status()
