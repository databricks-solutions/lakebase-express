"""Databricks workspace login (U2M OAuth) + connection status.

The browser is sent to the workspace's OAuth authorize endpoint; Databricks
redirects back to ``/api/databricks/oauth/callback`` with a code we exchange for
credentials. Pending consents are kept in memory keyed by the OAuth ``state``.

Note: the OAuth client's redirect URL must be allow-listed. The default public
``databricks-cli`` client accepts localhost redirects (local dev); for a deployed
app, register a custom OAuth app and set ``LBX_OAUTH_CLIENT_ID``/``_SECRET``.
"""
from __future__ import annotations

import logging

from databricks.sdk import WorkspaceClient
from databricks.sdk.oauth import Consent, OAuthClient
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backend import config

log = logging.getLogger("lakebase_express.databricks")

router = APIRouter(prefix="/api/databricks", tags=["databricks"])

# Pending OAuth consents keyed by `state` (short-lived, single-user app).
_PENDING: dict[str, dict] = {}


def _normalize_host(host: str) -> str:
    h = (host or "").strip().rstrip("/")
    if not h:
        return h
    if not h.startswith("http://") and not h.startswith("https://"):
        h = "https://" + h
    return h


class StartRequest(BaseModel):
    host: str


@router.get("/status")
def status() -> dict:
    return config.current_workspace()


@router.post("/oauth/start")
def oauth_start(req: StartRequest, request: Request) -> dict:
    host = _normalize_host(req.host)
    # Redirect back to this app's callback, on the origin the browser is using.
    origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
    redirect_url = f"{origin}/api/databricks/oauth/callback"

    client = OAuthClient.from_host(
        host=host,
        client_id=config.OAUTH_CLIENT_ID,
        redirect_url=redirect_url,
        scopes=config.OAUTH_SCOPES,
        client_secret=config.OAUTH_CLIENT_SECRET,
    )
    consent = client.initiate_consent()
    data = consent.as_dict()
    _PENDING[data["state"]] = {"host": host, "consent": data}
    return {"auth_url": data["authorization_url"]}


@router.get("/oauth/callback")
def oauth_callback(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    state = params.get("state")
    pending = _PENDING.pop(state, None) if state else None
    if not pending:
        return HTMLResponse(_close_html("Login failed — unknown or expired request."), status_code=400)
    try:
        consent = Consent.from_dict(pending["consent"])
        creds = consent.exchange_callback_parameters(params)
        host = pending["host"]
        user = WorkspaceClient(host=host, token=creds.token().access_token).current_user.me().user_name
        config.set_workspace_session(host, creds, user)
        return HTMLResponse(_close_html(f"Connected as {user}. You can close this window."))
    except Exception as exc:
        log.exception("OAuth exchange failed")
        return HTMLResponse(_close_html(f"Login failed — {exc}"), status_code=500)


@router.post("/logout")
def logout() -> dict:
    config.clear_workspace_session()
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


def _close_html(message: str) -> str:
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<body style='font-family:-apple-system,Segoe UI,sans-serif;padding:28px;color:#1b3139'>"
        f"<p>{message}</p>"
        "<script>try{window.opener&&window.opener.postMessage('lbx-databricks-auth','*')}catch(e){}"
        "setTimeout(function(){window.close()},900)</script></body>"
    )
