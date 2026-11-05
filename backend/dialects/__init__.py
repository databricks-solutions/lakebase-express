"""Source dialects, keyed by the catalog ``source_type`` (same ids as
``connectors/factory.py``). Adding a source: implement :class:`SourceDialect`,
register it here, and register its connector in the factory."""
from __future__ import annotations

from backend.dialects.base import SourceDialect
from backend.dialects.tsql import TSQL

__all__ = ["DEFAULT_DIALECT", "SourceDialect", "get_dialect", "supported_source_types"]

# Azure SQL and SQL Server share the T-SQL dialect.
_DIALECTS: dict[str, SourceDialect] = {
    "azure-sql": TSQL,
    "sql-server": TSQL,
}

# What callers get when they pass no dialect: the only source shipped before
# dialects existed, so existing call shapes keep their behaviour.
DEFAULT_DIALECT: SourceDialect = TSQL


def get_dialect(source_type: str) -> SourceDialect:
    """The dialect for a catalog source_type; ValueError if none is registered."""
    try:
        return _DIALECTS[(source_type or "").strip().lower()]
    except KeyError:
        raise ValueError(f"Source '{source_type}' is not supported yet.") from None


def supported_source_types() -> frozenset[str]:
    return frozenset(_DIALECTS)
