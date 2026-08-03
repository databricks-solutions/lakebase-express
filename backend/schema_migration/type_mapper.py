"""Authoritative SQL Server -> Postgres data type mapping.

`compatibility.py` decides *whether* a type is noteworthy; this module decides
*what it becomes*. The two share the same source-of-truth philosophy but serve
different phases (assessment vs. generation).
"""
from __future__ import annotations

from backend.assessment.models import ColumnInfo

# Simple, length-independent mappings.
_SIMPLE: dict[str, str] = {
    "bigint": "bigint",
    "int": "integer",
    "smallint": "smallint",
    "tinyint": "smallint",          # PG has no 1-byte int
    "bit": "boolean",
    "float": "double precision",
    "real": "real",
    "money": "numeric(19,4)",
    "smallmoney": "numeric(10,4)",
    "date": "date",
    "time": "time",
    "datetime": "timestamp(3)",
    "datetime2": "timestamp",
    "smalldatetime": "timestamp(0)",
    "datetimeoffset": "timestamptz",
    "uniqueidentifier": "uuid",
    "xml": "xml",
    "text": "text",
    "ntext": "text",
    "image": "bytea",
    "rowversion": "bytea",
    "timestamp": "bytea",           # SQL Server 'timestamp' == rowversion
    "sql_variant": "text",
    "hierarchyid": "text",
}

# Types whose Postgres form depends on length/precision/scale.
_PARAMETERIZED = {"varchar", "nvarchar", "char", "nchar", "decimal", "numeric", "varbinary", "binary"}


def map_type(col: ColumnInfo) -> str:
    """Return the Postgres column type for a scanned SQL Server column."""
    t = col.data_type.lower()

    if t in _SIMPLE:
        return _SIMPLE[t]

    if t in {"varchar", "nvarchar", "char", "nchar"}:
        # -1 == MAX in SQL Server -> unbounded text.
        if col.max_length in (None, -1):
            return "text"
        base = "varchar" if t in {"varchar", "nvarchar"} else "char"
        return f"{base}({col.max_length})"

    if t in {"decimal", "numeric"}:
        precision = col.precision or 18
        scale = col.scale or 0
        return f"numeric({precision},{scale})"

    if t in {"varbinary", "binary"}:
        return "bytea"

    # Unknown / spatial / CLR types: fall back to text and let the user review.
    return "text"


def is_parameterized(data_type: str) -> bool:
    return data_type.lower() in _PARAMETERIZED
