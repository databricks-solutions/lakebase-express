"""Source-connector factory — the seam for adding new source databases.

Maps a catalog ``source_type`` (the connector id from the frontend catalog) to a
concrete connector. Azure SQL and on-prem/IaaS SQL Server share the same T-SQL
dialect and driver (pymssql), so both route to ``AzureSqlConnection``.

To add a new source (Oracle, Postgres, …): implement a connector exposing
``database``, ``query(sql) -> list[dict]``, ``test_connection() -> bool`` and
``session()`` (a context manager yielding a connection-reusing ``query``) and
register its ``source_type`` here, then implement its ``SourceDialect`` (types,
expressions, collations) and register it under the same id in ``backend/dialects``.
"""
from __future__ import annotations

from backend.connectors.azure_sql import AzureSqlConnection

# T-SQL family: same catalog views, same driver.
_TSQL_SOURCES = {"azure-sql", "sql-server"}


def build_connector(
    source_type: str,
    *,
    host: str,
    database: str,
    username: str,
    password: str,
    port: int,
):
    """Return a source connector for the given catalog source_type.

    Raises ValueError for sources not yet enabled (the catalog shows these as
    "Coming soon").
    """
    if source_type in _TSQL_SOURCES:
        return AzureSqlConnection(
            host=host, database=database, username=username, password=password, port=port
        )
    raise ValueError(f"Source '{source_type}' is not supported yet.")
