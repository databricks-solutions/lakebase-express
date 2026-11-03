"""Reads what the live source and target allow, for the tuning recommender.

Best effort: every probe that fails leaves its values None and says why in
``notes``, and the recommender falls back to its defaults.
"""
from __future__ import annotations

import logging

from backend.data_migration.models import CapacityLimits

log = logging.getLogger("lakebase_express.capacity")

# Azure SQL Database: the service objective's own limits.
_AZURE_GOVERNANCE_SQL = (
    "SELECT * FROM sys.dm_user_db_resource_governance WHERE database_id = DB_ID()"
)
# The portal name (e.g. HS_PRMS_2); the DMV's slo_name is an internal one.
_SERVICE_OBJECTIVE_SQL = (
    "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'ServiceObjective') AS nvarchar(128)) AS slo"
)
# SQL Server / Managed Instance: the host's.
_SQLSERVER_SYSINFO_SQL = "SELECT cpu_count, max_workers_count FROM sys.dm_os_sys_info"

_TARGET_SQL = """
SELECT current_setting('max_connections')::int AS max_connections,
       current_setting('superuser_reserved_connections')::int
         + COALESCE(NULLIF(current_setting('reserved_connections', true), ''), '0')::int AS reserved,
       (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend')::int AS in_use
"""


def _positive(v) -> float | None:
    try:
        return float(v) if v is not None and float(v) > 0 else None
    except (TypeError, ValueError):
        return None


def _service_objective(conn) -> str | None:
    try:
        rows = conn.query(_SERVICE_OBJECTIVE_SQL, timeout=30)
        return (rows[0].get("slo") if rows else None) or None
    except Exception as exc:
        log.info("Service objective probe failed: %s", exc)
        return None


def probe_source(conn) -> dict:
    """CapacityLimits fields for the source, plus ``notes``."""
    try:
        rows = conn.query(_AZURE_GOVERNANCE_SQL, timeout=30)
        row = {k.lower(): v for k, v in (rows[0] if rows else {}).items()}
        cpus = _positive(row.get("cpu_limit"))
        workers = _positive(row.get("primary_group_max_workers"))
        if cpus or workers:
            return {
                "source_service_objective": _service_objective(conn) or row.get("slo_name") or None,
                "source_cpus": cpus,
                "source_max_workers": int(workers) if workers else None,
                "notes": [],
            }
    except Exception as exc:
        log.info("Azure SQL governance probe failed: %s", exc)
    try:
        rows = conn.query(_SQLSERVER_SYSINFO_SQL, timeout=30)
        row = rows[0] if rows else {}
        cpus = _positive(row.get("cpu_count"))
        workers = _positive(row.get("max_workers_count"))
        if cpus or workers:
            return {
                "source_cpus": cpus,
                "source_max_workers": int(workers) if workers else None,
                "notes": [],
            }
    except Exception as exc:
        log.info("SQL Server sys_info probe failed: %s", exc)
    return {"notes": [
        "Source CPU/worker limits could not be read (needs VIEW DATABASE STATE) — "
        "sized from the target alone."
    ]}


def probe_target(conn) -> dict:
    """CapacityLimits fields for the Lakebase target, plus ``notes``."""
    try:
        row = conn.query(_TARGET_SQL)[0]
        return {
            "target_max_connections": row["max_connections"],
            "target_reserved_connections": row["reserved"],
            "target_connections_in_use": row["in_use"],
            "notes": [],
        }
    except Exception as exc:
        log.info("Lakebase connection-limit probe failed: %s", exc)
        return {"notes": [f"Target connection limit could not be read ({exc})."]}


def probe(source, target) -> CapacityLimits:
    """Either side may be None (no password available) — reported as a note."""
    fields: dict = {"notes": []}
    for side, conn, fn in (("Source", source, probe_source), ("Target", target, probe_target)):
        if conn is None:
            fields["notes"].append(f"{side} not connected this session — its limits are unknown.")
            continue
        got = fn(conn)
        fields["notes"].extend(got.pop("notes"))
        fields.update(got)
    return CapacityLimits(**fields)
