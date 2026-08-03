"""Create/update the Databricks secret scope the generated snapshot job reads from.

The async snapshot notebook pulls the source and Lakebase passwords from a secret
scope at runtime (never embedded). This provisions that scope and (over)writes the
secret values so the user doesn't have to do it by hand in the workspace. Like the
other job/async paths it needs a live workspace and is isolated from the tested core.
"""
from __future__ import annotations

import logging

from databricks.sdk.errors import ResourceAlreadyExists

from backend.config import workspace_client

log = logging.getLogger("lakebase_express.secret_setup")


def ensure_secret_scope(scope: str, secrets: dict[str, str]) -> dict:
    """Ensure ``scope`` exists and upsert each ``key -> value`` in ``secrets``.

    A scope that already exists is reused (not an error); ``put_secret`` creates or
    overwrites each value, so existing secrets are updated in place. Empty values are
    skipped (nothing to store). Returns which keys were written and whether the scope
    was newly created.
    """
    w = workspace_client()
    created = False
    try:
        w.secrets.create_scope(scope=scope)
        created = True
    except ResourceAlreadyExists:  # reuse it; put_secret below updates the values
        log.info("Secret scope %s already exists — updating secret values", scope)

    written: list[str] = []
    for key, value in secrets.items():
        if not value:
            continue
        w.secrets.put_secret(scope=scope, key=key, string_value=value)
        written.append(key)
    return {"scope": scope, "created": created, "keys": sorted(written)}
