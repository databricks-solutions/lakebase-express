"""Source-side projections for SQL Server types the readers can't fetch raw.

pymssql (Sync mode) and Spark's JDBC reader (Async mode, UNRECOGNIZED_SQL_TYPE)
both fail on these CLR-backed types, so SQL Server converts each one in the
SELECT list into the text form the target column type parses on COPY
(schema_migration/type_mapper decides those types). Templates take ``{c}`` (the
quoted column) and ``{a}`` (its alias, the original name).
"""
from __future__ import annotations

# Exact formatting per base type; the default string conversion rounds float,
# money and datetime values.
_VARIANT_VALUE = (
    "CASE CAST(SQL_VARIANT_PROPERTY({c}, 'BaseType') AS NVARCHAR(128)) "
    "WHEN 'float' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS FLOAT), 3) "
    "WHEN 'real' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS REAL), 3) "
    "WHEN 'money' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS MONEY), 2) "
    "WHEN 'smallmoney' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS SMALLMONEY), 2) "
    # Direct: via datetime2, datetime's 1/300 s ticks show as .9966667, not .997.
    "WHEN 'datetime' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS DATETIME), 126) "
    "WHEN 'smalldatetime' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS SMALLDATETIME), 126) "
    # A variant converts with style 0 ('Jan  2 2024  3:04AM') even for these types.
    "WHEN 'datetime2' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS DATETIME2), 126) "
    "WHEN 'date' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS DATE), 23) "
    "WHEN 'time' THEN CAST(CAST({c} AS TIME) AS NVARCHAR(MAX)) "
    "WHEN 'datetimeoffset' THEN CAST(CAST({c} AS DATETIMEOFFSET) AS NVARCHAR(MAX)) "
    "WHEN 'binary' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS VARBINARY(8000)), 1) "
    "WHEN 'varbinary' THEN CONVERT(NVARCHAR(MAX), CAST({c} AS VARBINARY(8000)), 1) "
    "ELSE CAST({c} AS NVARCHAR(MAX)) END"
)

SOURCE_CASTS: dict[str, str] = {
    # jsonb {"type": <base type>, "value": <exact string>}, so typed reads can recover it.
    "sql_variant": (
        "CASE WHEN {c} IS NULL THEN NULL ELSE N'{{\"type\":\"' + "
        "CAST(SQL_VARIANT_PROPERTY({c}, 'BaseType') AS NVARCHAR(128)) + "
        "N'\",\"value\":\"' + STRING_ESCAPE(" + _VARIANT_VALUE + ", 'json') + N'\"}}' END AS {a}"
    ),
    # ltree: '/1/2.5/' -> '1.2_5' (ltree labels can't hold '.'); the root '/' -> ''.
    "hierarchyid": (
        "CASE WHEN {c}.GetLevel() = 0 THEN '' ELSE REPLACE(SUBSTRING("
        "REPLACE({c}.ToString(), '.', '_'), 2, LEN({c}.ToString()) - 2), '/', '.') END AS {a}"
    ),
    # Hex WKB with an SRID prefix: exact coordinates, Z/M kept; PostGIS geometry parses it.
    "geometry": (
        "'SRID=' + CAST({c}.STSrid AS VARCHAR(11)) + ';' + "
        "CONVERT(VARCHAR(MAX), {c}.AsBinaryZM(), 2) AS {a}"
    ),
    # EWKT: PostGIS geography only takes an SRID prefix on WKT input. FullGlobe has
    # no PostGIS form, so it loads as NULL and the run reports how many.
    "geography": (
        "CASE WHEN {c}.STGeometryType() = 'FullGlobe' THEN NULL ELSE "
        "'SRID=' + CAST({c}.STSrid AS VARCHAR(11)) + ';' + {c}.AsTextZM() END AS {a}"
    ),
}

# User CLR types: every UDT implements ToString(); the plan maps them to text.
CLR_CAST = "{c}.ToString() AS {a}"


def cast_template(data_type: str, is_clr_type: bool = False) -> str | None:
    """The projection for a column, or None to select it as is."""
    return SOURCE_CASTS.get(data_type.lower()) or (CLR_CAST if is_clr_type else None)


def full_globe_sql(schema: str, table: str, columns: list[str]) -> str:
    """One row: the FullGlobe count of each geography column, aliased by name."""
    def q(name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"
    sums = ", ".join(
        f"SUM(CASE WHEN {q(c)}.STGeometryType() = 'FullGlobe' THEN 1 ELSE 0 END) AS {q(c)}"
        for c in columns
    )
    return f"SELECT {sums} FROM {q(schema)}.{q(table)}"


def full_globe_note(counts: dict[str, int]) -> str | None:
    """The run's note for the FullGlobe values stored as NULL, if any."""
    found = {c: n for c, n in counts.items() if n}
    if not found:
        return None
    cols = ", ".join(f"{n:,} in {c}" for c, n in found.items())
    return f"FULLGLOBE has no PostGIS form; stored as NULL: {cols}."


def full_globe_error(table: str, not_null: list[str]) -> str:
    return (f"{table}: FULLGLOBE values in NOT NULL column(s) {', '.join(not_null)} "
            "have no PostGIS form. Make the column nullable or change those rows.")
