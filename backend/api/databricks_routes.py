"""Databricks connection status + secret-scope listing for the bound workspace.

The app is bound to exactly one workspace for the life of the process — the CLI
profile it was started with locally, or the workspace the Databricks App is
published in. There is no in-app login or workspace switching; see
``backend.config``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from backend import config

log = logging.getLogger("lakebase_express.databricks")

router = APIRouter(prefix="/api/databricks", tags=["databricks"])


@router.get("/status")
def status() -> dict:
    return config.current_workspace()


# --- Secret scopes (for the password-source dropdowns) ----------------------------
#
# Populate the Secret-mode scope/key pickers so the user selects a scope + key
# instead of typing them. Cloud-agnostic — lists whatever scopes the workspace
# has (Databricks-native everywhere; Key Vault-backed too on Azure). All fail-soft:
# on any error (no workspace auth, missing list permission) they return an empty
# list so the UI falls back to manual entry rather than breaking the connection form.


@router.get("/secret-scopes")
def secret_scopes() -> dict:
    """List secret scopes visible to the app, tagged by backend so the UI can
    label Key Vault-backed scopes (only present on Azure workspaces)."""
    try:
        scopes = config.workspace_client().secrets.list_scopes()
    except Exception as exc:
        log.info("Could not list secret scopes (falling back to manual entry): %s", exc)
        return {"scopes": []}
    out = []
    for s in scopes or []:
        backend = getattr(s.backend_type, "value", None) or str(s.backend_type or "")
        out.append({"name": s.name, "backend_type": backend})
    return {"scopes": out}


@router.get("/secret-scopes/{scope}/keys")
def secret_keys(scope: str) -> dict:
    """List the secret keys within a scope. For a Key Vault-backed scope these are
    the Key Vault secret names."""
    try:
        secrets = config.workspace_client().secrets.list_secrets(scope=scope)
    except Exception as exc:
        log.info("Could not list secrets for scope %s (manual entry): %s", scope, exc)
        return {"keys": []}
    return {"keys": [s.key for s in (secrets or []) if s.key]}


@router.get("/secret-scopes/{scope}/keys/{key}/preview")
def secret_preview(scope: str, key: str) -> dict:
    """Inspect a secret so the form can auto-fill connection fields.

    The secret may hold a bare password (nothing to fill) or a whole connection
    string, in which case we return its non-secret coordinates (host / database /
    port / username / sslmode) — NEVER the password. ``is_connection_string``
    lets the UI decide whether to prompt to apply the parsed fields.
    """
    from backend.assessment.models import SecretRef
    from backend.connectors.credentials import resolve_secret_ref_parsed

    parsed = resolve_secret_ref_parsed(SecretRef(scope=scope, key=key))
    if parsed is None:
        return {"ok": False, "is_connection_string": False}
    return {
        "ok": True,
        "is_connection_string": parsed.is_connection_string,
        # Password intentionally omitted — it never leaves the backend.
        "host": parsed.host,
        "database": parsed.database,
        "port": parsed.port,
        "username": parsed.username,
        "sslmode": parsed.sslmode,
    }
