"""Databricks identity (OAuth) auth for the Lakebase target.

Deployed, the target connects as the signed-in user: the Apps proxy forwards
their token (needs the ``postgres`` user scope) and it mints a one-hour Lakebase
credential for that user's own Postgres role. Locally there is no proxy, so the
CLI profile is the user.
"""
from __future__ import annotations

import base64
import contextvars
import json
import logging
import os
import threading
import time

from databricks.sdk import WorkspaceClient

from backend.config import _host_label, find_lakebase_endpoint, workspace_client

log = logging.getLogger("lakebase_express.lakebase_identity")

AUTH_PASSWORD = "password"
AUTH_DATABRICKS = "databricks"

TOKEN_HEADER = "x-forwarded-access-token"
EMAIL_HEADER = "x-forwarded-email"

# Credentials last an hour and are checked only at login; re-mint well before.
_CREDENTIAL_TTL_S = 45 * 60

# Set per request by backend/main.py: (email, forwarded token).
_request: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "lbx_lakebase_user", default=None
)

_lock = threading.Lock()
_latest_token: dict[str, str] = {}        # email -> newest forwarded token
_user_names: dict[str, str] = {}          # email -> Databricks user_name (the role)
_emails: dict[str, str] = {}              # user_name -> email
_endpoints: dict[str, str] = {}           # host -> endpoint path (hits only)
_credentials: dict[tuple[str, str], tuple[str, float]] = {}  # (user, host) -> (token, minted)


class IdentityAuthError(RuntimeError):
    """The target cannot authenticate as the signed-in Databricks user."""


def running_in_app() -> bool:
    return bool(os.getenv("DATABRICKS_APP_NAME"))


def bind_request(email: str | None, token: str | None) -> contextvars.Token:
    """Remember the request's forwarded token. Every request refreshes it, so a
    long run started by this user keeps minting while their tab polls."""
    if email and token:
        with _lock:
            _latest_token[email] = token
        return _request.set((email, token))
    return _request.set(None)


def reset_request(tok: contextvars.Token) -> None:
    _request.reset(tok)


def _user_client(token: str) -> WorkspaceClient:
    return WorkspaceClient(host=workspace_client().config.host, token=token, auth_type="pat")


def current_identity() -> str:
    """The Databricks user an identity-mode target connects as, for this request."""
    req = _request.get()
    if req is None:
        if running_in_app():
            raise IdentityAuthError(
                "No signed-in Databricks user on this request. Add the `postgres` user "
                "authorization scope to the app, then reopen it so you can consent."
            )
        from backend.config import current_workspace

        ws = current_workspace()
        if not ws.get("connected"):
            raise IdentityAuthError(f"No Databricks identity available: {ws.get('error')}")
        return ws["user"]
    email, token = req
    with _lock:
        name = _user_names.get(email)
    if name is None:
        # The role is the user_name, which can differ from the email (e.g. an Azure UPN).
        try:
            name = _user_client(token).current_user.me().user_name or email
        except Exception as exc:
            log.warning("Could not resolve the user name for %s, using the email: %s", email, exc)
            name = email
        with _lock:
            _user_names[email] = name
            _emails[name] = email
    return name


def _client_for(user: str) -> WorkspaceClient:
    if not running_in_app():
        return workspace_client()
    with _lock:
        token = _latest_token.get(_emails.get(user, user))
    if not token:
        raise IdentityAuthError(
            f"No Databricks sign-in for {user} is available to this app process — "
            "reopen the app and retry."
        )
    return _user_client(token)


def _granted_scopes(token: str) -> str:
    """The scopes a forwarded JWT carries, for error messages; "" if unreadable."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        scopes = claims.get("scope") or claims.get("scp") or ""
        return " ".join(scopes) if isinstance(scopes, list) else str(scopes)
    except Exception:
        return ""


def _sdk_error(w: WorkspaceClient, what: str, exc: Exception) -> IdentityAuthError:
    """Readable error for a failed SDK call; drops the SDK's config dump."""
    detail = str(exc).split(" Config: ")[0]
    if "does not have required scopes" in detail:
        granted = _granted_scopes(w.config.token or "") if running_in_app() else ""
        return IdentityAuthError(
            "Your sign-in to this app doesn't include the `postgres` scope"
            + (f" (it has: {granted})" if granted else "")
            + ". Usually the sign-in predates the scope: open the app in a private window, "
            "or sign out and back in, and accept the consent prompt."
        )
    return IdentityAuthError(f"{what}: {detail}")


def _endpoint(w: WorkspaceClient, host: str) -> str:
    key = _host_label(host).lower()
    with _lock:
        cached = _endpoints.get(key)
    if cached:
        return cached
    override = os.getenv("LBX_TARGET_PG_ENDPOINT", "").strip()
    try:
        found = override or find_lakebase_endpoint(w, host)
    except Exception as exc:
        raise _sdk_error(w, f"Could not list Lakebase projects to find the endpoint for {key}",
                         exc) from exc
    if not found:
        raise IdentityAuthError(
            f"No Lakebase endpoint you can see serves {key}. Check the host, and that "
            "you have access to its Lakebase project."
        )
    with _lock:
        _endpoints[key] = found
    return found


def credential(user: str, host: str) -> str:
    """A Lakebase password (OAuth token) for ``user`` on ``host``, cached until
    shortly before it expires."""
    key = (user, _host_label(host).lower())
    with _lock:
        hit = _credentials.get(key)
    if hit and time.monotonic() - hit[1] < _CREDENTIAL_TTL_S:
        return hit[0]
    w = _client_for(user)
    endpoint = _endpoint(w, host)
    try:
        token = w.postgres.generate_database_credential(endpoint=endpoint).token
    except Exception as exc:
        raise _sdk_error(w, f"Could not get a Lakebase credential for {user} (if this "
                         "happened during a long run, your sign-in expired — keep the app "
                         "open while it runs)", exc) from exc
    with _lock:
        _credentials[key] = (token, time.monotonic())
    return token


def resolve_endpoint(user: str, host: str) -> str:
    """The endpoint path for ``host``, looked up as ``user``."""
    return _endpoint(_client_for(user), host)


def endpoint_for(host: str) -> str:
    """The cached endpoint path for ``host``, or "" — for error hints only."""
    with _lock:
        return _endpoints.get(_host_label(host).lower(), "")


def create_role_snippet(identity: str, identity_type: str, endpoint: str,
                        where: str = "the Lakebase project") -> str:
    """What a project owner runs to give an identity a Lakebase role. Needs a session
    authenticated as a Databricks identity (``databricks psql``)."""
    project = endpoint.split("/branches/")[0].removeprefix("projects/") or "<project-id>"
    # Doubled, or a quote in an identity would break the snippet.
    literal = identity.replace("'", "''")
    return (
        f"-- Run against {where}:\n"
        f"--   databricks psql --project {project}\n"
        "CREATE EXTENSION IF NOT EXISTS databricks_auth;\n"
        f"SELECT databricks_create_role('{literal}', '{identity_type}');"
    )


def missing_role_error(user: str, host: str, exc: BaseException) -> IdentityAuthError:
    return IdentityAuthError(
        f"Lakebase rejected {user}: {exc}. Your Databricks identity needs its own "
        "Postgres role there — the project owner can create it with:\n"
        + create_role_snippet(user, "USER", endpoint_for(host))
    )


def clear() -> None:
    """Drop every cached token and credential (tests)."""
    with _lock:
        for d in (_latest_token, _user_names, _emails, _endpoints, _credentials):
            d.clear()
