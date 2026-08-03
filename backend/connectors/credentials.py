"""Connection-password cache for source and Lakebase credentials.

Projects intentionally never persist passwords (see ``backend/projects/models.py``);
the UI supplies them per browser session. But the SPA's copy is lost on any page
reload or navigation, and requests then arrive with an empty password — the scan
fails with a confusing driver error even though the user "saved" the connection
minutes earlier.

The backend therefore remembers the last password that *worked* for a given
(project_id, source_type, host, database, username) and falls back to it when a
request omits the password. Credentials are scoped to the migration project, so
two projects pointing at the same host never share a stored password; an empty
``project_id`` keeps the legacy project-agnostic behaviour. A wrong password is
never cached because callers only ``remember_password`` after a successful
connection.

Where that password lives depends on the configured backend (see
``credential_store.py``): the in-process memory cache by default, or a
Lakebase-backed store that keeps the password **Fernet-encrypted, never in clear
text** so it survives an App restart. Either way the public API here is unchanged.
"""
from __future__ import annotations

import base64
import logging
import os

from backend.assessment.models import SecretRef
from backend.connectors.conn_string import ParsedSecret, parse_secret_value
from backend.connectors.credential_store import (
    CredKey,
    PostgresCredentialStore,
    get_credential_store,
)

log = logging.getLogger("lakebase_express.credentials")

# Cache namespace for the Lakebase target — source entries use their source_type.
LAKEBASE_NAMESPACE = "lakebase"


def _key(source_type: str, host: str, database: str, username: str, project_id: str) -> CredKey:
    # Hostnames are case-insensitive; database and login names are matched
    # verbatim (SQL Server treats them case-insensitively, but normalizing the
    # lookup key only would break round-trips for exotic collations). project_id
    # scopes the credential to one project (verbatim; "" = project-agnostic).
    return (project_id.strip(), source_type.strip().lower(), host.strip().lower(),
            database.strip(), username.strip())


def remember_password(source_type: str, host: str, database: str, username: str, password: str,
                      project_id: str = "") -> None:
    """Cache a password that just authenticated successfully. Empty values are ignored."""
    if not password:
        return
    try:
        get_credential_store().remember(_key(source_type, host, database, username, project_id), password)
    except Exception as exc:  # persistence must never break the request path
        log.warning("Could not persist credential: %s", exc)


def resolve_password(source_type: str, host: str, database: str, username: str,
                     project_id: str = "") -> str | None:
    """Return the cached password for this connection, or None."""
    try:
        return get_credential_store().resolve(_key(source_type, host, database, username, project_id))
    except Exception as exc:
        log.warning("Could not resolve stored credential: %s", exc)
        return None


# --- Secret-manager references ------------------------------------------------------
#
# A SecretRef names where a password lives instead of carrying it. Both kinds
# resolve through the Databricks Secrets API: a plain Databricks scope directly,
# and an Azure Key Vault through a Databricks Key Vault-backed secret scope (an
# Azure-native feature the user registers once). The reference itself is not
# secret, so it can be persisted in clear text and re-resolved live each session.


def _normalize_workspace_host(host: str | None) -> str:
    """Canonical host label used to bind a SecretRef to one workspace."""
    value = (host or "").strip().rstrip("/").lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value


def _read_secret_value(ref: SecretRef) -> str | None:
    """Read and decode the raw secret value named by ``ref`` from the Databricks
    Secrets API. Returns None (never raises) so a bad reference degrades to "no
    password" rather than a 500 — the caller surfaces a clean 400 instead."""
    from backend.config import workspace_client

    try:
        client = workspace_client()
    except Exception as exc:
        log.warning("Could not initialize workspace client for secret %s/%s: %s", ref.scope, ref.key, exc)
        return None
    expected_host = _normalize_workspace_host(ref.workspace_host)
    active_host = _normalize_workspace_host(client.config.host)
    if expected_host and expected_host != active_host:
        log.warning(
            "Refusing to resolve secret %s/%s: reference workspace %s does not match active workspace %s",
            ref.scope, ref.key, expected_host, active_host or "<unknown>",
        )
        return None
    try:
        resp = client.secrets.get_secret(scope=ref.scope, key=ref.key)
    except Exception as exc:
        log.warning("Could not read secret %s/%s: %s", ref.scope, ref.key, exc)
        return None
    value = resp.value
    if not value:
        return None
    # The Secrets API returns the value base64-encoded (same handling as the
    # project store and the Fernet-key reader).
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        # Already-plain value — return as-is rather than fail.
        return value


def resolve_secret_ref_parsed(ref: SecretRef) -> ParsedSecret | None:
    """Resolve ``ref`` and parse it: a bare password comes back as a
    ``ParsedSecret`` with just the password, a full connection string as one with
    host/database/port/etc. too. None when the secret can't be read."""
    value = _read_secret_value(ref)
    if value is None:
        return None
    return parse_secret_value(value)


def resolve_secret_ref(ref: SecretRef) -> str | None:
    """Resolve ``ref`` to the password to authenticate with. If the secret holds a
    whole connection string, the password is parsed out of it; a bare password is
    returned as-is. None when the secret can't be read."""
    parsed = resolve_secret_ref_parsed(ref)
    return parsed.password if parsed else None


def remember_secret_ref(source_type: str, host: str, database: str, username: str,
                        ref: SecretRef, project_id: str = "") -> None:
    """Persist the secret-manager pointer for this connection (not its value), so a
    resumed session or redeployed App re-resolves it without re-entry."""
    try:
        get_credential_store().remember_ref(
            _key(source_type, host, database, username, project_id),
            (ref.kind, ref.scope, ref.key, ref.workspace_host),
        )
    except Exception as exc:  # persistence must never break the request path
        log.warning("Could not persist secret reference: %s", exc)


def resolve_stored_secret_ref(source_type: str, host: str, database: str, username: str,
                             project_id: str = "") -> SecretRef | None:
    """Return the secret-manager pointer stored for this connection, or None."""
    try:
        stored = get_credential_store().resolve_ref(
            _key(source_type, host, database, username, project_id)
        )
    except Exception as exc:
        log.warning("Could not resolve stored secret reference: %s", exc)
        return None
    if not stored:
        return None
    kind, scope, key, workspace_host = stored
    return SecretRef(kind=kind, scope=scope, key=key, workspace_host=workspace_host)


def resolve_effective_password(
    source_type: str, host: str, database: str, username: str, project_id: str,
    typed_password: str, secret_ref: SecretRef | None,
) -> tuple[str | None, SecretRef | None]:
    """Resolve the password to authenticate with, by precedence:

      1. ``typed_password`` — entered inline this request;
      2. ``secret_ref`` — a secret-manager pointer sent with this request;
      3. a secret-manager pointer persisted for this connection (re-resolved live);
      4. the plaintext password cached from the last successful connection.

    Returns ``(password, ref)`` where ``ref`` is the reference that produced the
    password (so the caller persists the pointer, not the value) or None when the
    password was typed/cached. ``password`` is None only when nothing resolved.
    """
    if typed_password:
        return typed_password, None
    if secret_ref is not None:
        return resolve_secret_ref(secret_ref), secret_ref
    stored_ref = resolve_stored_secret_ref(source_type, host, database, username, project_id)
    if stored_ref is not None:
        return resolve_secret_ref(stored_ref), stored_ref
    return resolve_password(source_type, host, database, username, project_id), None


def remember_effective(
    source_type: str, host: str, database: str, username: str, project_id: str,
    password: str, ref: SecretRef | None,
) -> None:
    """Persist what authenticated: the secret-manager pointer if one was used,
    else the plaintext password. Called only after a successful connection."""
    if ref is not None:
        remember_secret_ref(source_type, host, database, username, ref, project_id)
    else:
        remember_password(source_type, host, database, username, password, project_id)


def clear() -> None:
    """Drop all cached passwords (test seam)."""
    get_credential_store().clear()


def clear_project(project_id: str) -> None:
    """Delete every stored credential scoped to one migration project.

    Unlike request-time credential persistence, deletion is not fail-soft: the
    project DELETE endpoint must keep the project when durable credential cleanup
    is unavailable, otherwise it would recreate the orphaned-row problem this
    operation exists to prevent.
    """
    store = get_credential_store()
    if (
        os.getenv("LBX_PROJECTS_BACKEND", "local").lower() == "postgres"
        and not isinstance(store, PostgresCredentialStore)
    ):
        raise RuntimeError("Postgres credential store is unavailable")
    store.clear_project(project_id)
