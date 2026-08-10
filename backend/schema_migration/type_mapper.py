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

# Postgres caps timestamp/time precision at microseconds; T-SQL datetime2/time go
# to 100 ns (7). A literal datetime2(7) -> timestamp(7) is rejected outright, so
# out-of-range precision is dropped rather than carried over.
_MAX_TIME_PRECISION = 6

# CONVERT()/CAST() target types that are safe to translate inside an expression,
# where no length/precision is involved.
_CAST_SIMPLE: dict[str, str] = {
    "datetime2": "timestamp",
    "datetime": "timestamp(3)",
    "smalldatetime": "timestamp(0)",
    "datetimeoffset": "timestamptz",
    "date": "date",
    "time": "time",
    "int": "integer",
    "bigint": "bigint",
    "smallint": "smallint",
    "tinyint": "smallint",
    "bit": "boolean",
    "uniqueidentifier": "uuid",
    "float": "double precision",
    "real": "real",
    "money": "numeric(19,4)",
    "smallmoney": "numeric(10,4)",
    "text": "text",
    "ntext": "text",
    "xml": "xml",
}

# Types whose T-SQL default when written bare differs from the Postgres default in
# a way that loses data, so a bare occurrence must not be translated:
#   char/nchar  -> T-SQL char(30), Postgres char(1)      (truncates to 1 char)
#   decimal     -> T-SQL decimal(18,0), Postgres numeric (T-SQL drops the fraction)
# With an explicit length/precision they are unambiguous and map normally.
_UNSAFE_BARE = {"char", "nchar", "decimal", "numeric"}


def map_cast_type(type_name: str, params: list[str] | None = None) -> str | None:
    """Return the Postgres spelling of a CONVERT()/CAST() target type, or None if
    it can't be translated faithfully.

    Unlike :func:`map_type`, this declines (None) instead of falling back to
    ``text``: a cast target is chosen deliberately, so guessing would silently
    change what the expression computes. Declining leaves the expression verbatim
    so it fails visibly at apply time and can be corrected in the plan.

    ``params`` holds the parenthesised arguments as written, e.g. ``["7"]`` for
    ``datetime2(7)`` or ``["18", "2"]`` for ``decimal(18,2)``.
    """
    t = type_name.lower()
    args = [p.strip().lower() for p in (params or []) if p.strip()]

    if t in _UNSAFE_BARE and not args:
        return None

    if t in {"time", "datetime2"} and args:
        # Honour an in-range precision; drop 7 (and anything odd) to the base type.
        base = "time" if t == "time" else "timestamp"
        if args[0].isdigit() and int(args[0]) <= _MAX_TIME_PRECISION:
            return f"{base}({args[0]})"
        return base

    if t in _CAST_SIMPLE:
        return _CAST_SIMPLE[t]

    if t in {"varchar", "nvarchar", "char", "nchar"}:
        if args and args[0] == "max":
            return "text"
        base = "varchar" if t in {"varchar", "nvarchar"} else "char"
        # Bare varchar/nvarchar widens (T-SQL 30 chars -> unbounded); that can't
        # lose data, so it maps. Bare char/nchar is refused above.
        return f"{base}({args[0]})" if args and args[0].isdigit() else base

    if t in {"decimal", "numeric"}:
        precision = args[0]
        scale = args[1] if len(args) > 1 else "0"
        if precision.isdigit() and scale.isdigit():
            return f"numeric({precision},{scale})"
        return None

    if t in {"varbinary", "binary"}:
        return "bytea"

    # Spatial, CLR, sql_variant, hierarchyid, anything unrecognised: decline.
    return None


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
