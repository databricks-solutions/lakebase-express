"""Orchestrates the Azure SQL source scan and produces an AssessmentReport.

All extraction uses Azure SQL system catalog views so it works on any database
without prior knowledge of the schema:
  * INFORMATION_SCHEMA.TABLES / .COLUMNS   -> structure
  * sys.dm_db_partition_stats              -> approximate row counts (cheap)
  * sys.sql_modules + sys.objects          -> programmable object definitions
"""
from __future__ import annotations

from backend.assessment import compatibility, report
from backend.assessment.models import (
    AssessmentReport,
    CheckConstraintInfo,
    ColumnDefaultInfo,
    ColumnInfo,
    ForeignKeyInfo,
    IndexColumnInfo,
    IndexInfo,
    ProgrammableObject,
    TableInfo,
)
from backend.connectors.azure_sql import AzureSqlConnection

# --- Catalog queries -------------------------------------------------------------
#
# Only USER objects are scanned. SQL Server ships hundreds of system tables,
# views, procedures, and functions; migrating any of them is wrong. We exclude:
#   * system schemas (sys, INFORMATION_SCHEMA, guest, db_* fixed roles)
#   * Microsoft-shipped objects (sys.objects.is_ms_shipped = 1)
# This is what prevented (e.g.) a system view from being picked up for translation.

# Schemas that are never user content.
_SYSTEM_SCHEMAS = (
    "'sys'", "'INFORMATION_SCHEMA'", "'guest'",
    "'db_owner'", "'db_accessadmin'", "'db_securityadmin'", "'db_ddladmin'",
    "'db_backupoperator'", "'db_datareader'", "'db_datawriter'",
    "'db_denydatareader'", "'db_denydatawriter'",
)
_SCHEMA_EXCLUSION = ", ".join(_SYSTEM_SCHEMAS)

# COLLATION_NAME (NULL for non-character columns) decides comparison semantics: a
# ..._CI_AS column compares case-insensitively where Postgres defaults to
# case-sensitive, so the migration mirrors it (schema_migration/collation_mapper).
_COLUMNS_SQL = f"""
SELECT  c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE,
        c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
        c.IS_NULLABLE, c.COLLATION_NAME
FROM    INFORMATION_SCHEMA.COLUMNS c
JOIN    sys.tables t      ON t.name = c.TABLE_NAME
JOIN    sys.schemas s     ON s.schema_id = t.schema_id AND s.name = c.TABLE_SCHEMA
WHERE   t.is_ms_shipped = 0
  AND   c.TABLE_SCHEMA NOT IN ({_SCHEMA_EXCLUSION})
"""

# Sum of rows across heap/clustered-index partitions (index_id 0/1). Fast and
# avoids COUNT(*) over every table.
_ROWCOUNTS_SQL = f"""
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME,
        SUM(p.row_count) AS ROW_COUNT
FROM    sys.dm_db_partition_stats p
JOIN    sys.tables t  ON t.object_id = p.object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
WHERE   p.index_id IN (0, 1)
  AND   t.is_ms_shipped = 0
  AND   s.name NOT IN ({_SCHEMA_EXCLUSION})
GROUP BY s.name, t.name
"""

_MODULES_SQL = f"""
SELECT  s.name AS SCHEMA_NAME, o.name AS OBJECT_NAME, o.type_desc AS OBJECT_TYPE,
        m.definition AS DEFINITION
FROM    sys.sql_modules m
JOIN    sys.objects o ON o.object_id = m.object_id
JOIN    sys.schemas s ON s.schema_id = o.schema_id
WHERE   o.type IN ('P', 'V', 'FN', 'IF', 'TF', 'TR')
  AND   o.is_ms_shipped = 0
  AND   s.name NOT IN ({_SCHEMA_EXCLUSION})
"""

# Primary-key columns per table, in key order. Assessment metadata surfaced in
# the UI; a PK also enables parallel/partitioned reads when scaling the load.
_PRIMARY_KEYS_SQL = f"""
SELECT  kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.ORDINAL_POSITION
FROM    INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN    INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        ON  kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
        AND kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
WHERE   tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
  AND   kcu.TABLE_SCHEMA NOT IN ({_SCHEMA_EXCLUSION})
ORDER BY kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.ORDINAL_POSITION
"""

# Foreign keys, one row per constraint column (in constraint-column order).
# Feeds the post-data phase of the migration plan (FKs are created after the
# data load, for bulk-load performance and referenced-row availability).
_FOREIGN_KEYS_SQL = f"""
SELECT  s.name  AS TABLE_SCHEMA, t.name  AS TABLE_NAME, fk.name AS FK_NAME,
        pc.name AS COLUMN_NAME,
        rs.name AS REF_SCHEMA,   rt.name AS REF_TABLE,  rc.name AS REF_COLUMN,
        fk.delete_referential_action_desc AS ON_DELETE,
        fk.update_referential_action_desc AS ON_UPDATE
FROM    sys.foreign_keys fk
JOIN    sys.tables  t  ON t.object_id  = fk.parent_object_id
JOIN    sys.schemas s  ON s.schema_id  = t.schema_id
JOIN    sys.tables  rt ON rt.object_id = fk.referenced_object_id
JOIN    sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN    sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN    sys.columns pc ON pc.object_id = fkc.parent_object_id     AND pc.column_id = fkc.parent_column_id
JOIN    sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
WHERE   t.is_ms_shipped = 0
  AND   fk.is_ms_shipped = 0
  AND   s.name NOT IN ({_SCHEMA_EXCLUSION})
ORDER BY s.name, t.name, fk.name, fkc.constraint_column_id
"""

# Rowstore indexes (clustered + nonclustered), one row per index column. Unique
# CONSTRAINTS are included (is_unique_constraint = 1) and become unique indexes
# on Postgres — a unique index satisfies FK references just like a constraint.
# PK-backing indexes are excluded (the PK is recreated as a real constraint);
# columnstore/XML/spatial/hypothetical/disabled indexes have no direct Postgres
# equivalent and are skipped.
_INDEXES_SQL = f"""
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME, i.name AS INDEX_NAME,
        i.is_unique AS IS_UNIQUE, i.filter_definition AS FILTER_DEFINITION,
        c.name AS COLUMN_NAME, ic.key_ordinal AS KEY_ORDINAL,
        ic.is_descending_key AS IS_DESCENDING, ic.is_included_column AS IS_INCLUDED
FROM    sys.indexes i
JOIN    sys.tables  t ON t.object_id = i.object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
JOIN    sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN    sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE   i.type IN (1, 2)
  AND   i.is_primary_key = 0
  AND   i.is_hypothetical = 0
  AND   i.is_disabled = 0
  AND   t.is_ms_shipped = 0
  AND   s.name NOT IN ({_SCHEMA_EXCLUSION})
ORDER BY s.name, t.name, i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id
"""

# Column DEFAULT constraints (raw T-SQL expressions; translated mechanically
# when the plan is built).
_DEFAULTS_SQL = f"""
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME,
        c.name AS COLUMN_NAME, dc.definition AS DEFINITION
FROM    sys.default_constraints dc
JOIN    sys.tables  t ON t.object_id = dc.parent_object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
JOIN    sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
WHERE   t.is_ms_shipped = 0
  AND   s.name NOT IN ({_SCHEMA_EXCLUSION})
ORDER BY s.name, t.name, c.name
"""

# Enabled CHECK constraints (raw T-SQL predicates).
_CHECKS_SQL = f"""
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME,
        cc.name AS CHECK_NAME, cc.definition AS DEFINITION
FROM    sys.check_constraints cc
JOIN    sys.tables  t ON t.object_id = cc.parent_object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
WHERE   cc.is_disabled = 0
  AND   t.is_ms_shipped = 0
  AND   s.name NOT IN ({_SCHEMA_EXCLUSION})
ORDER BY s.name, t.name, cc.name
"""

# IDENTITY columns (at most one per table) — become Postgres identity columns
# (or a sequence-backed default for non-integer types) after the data load.
_IDENTITY_SQL = f"""
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME, ic.name AS COLUMN_NAME
FROM    sys.identity_columns ic
JOIN    sys.tables  t ON t.object_id = ic.object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
WHERE   t.is_ms_shipped = 0
  AND   s.name NOT IN ({_SCHEMA_EXCLUSION})
"""

_TYPE_DESC_MAP = {
    "SQL_STORED_PROCEDURE": "PROCEDURE",
    "VIEW": "VIEW",
    "SQL_SCALAR_FUNCTION": "FUNCTION",
    "SQL_INLINE_TABLE_VALUED_FUNCTION": "FUNCTION",
    "SQL_TABLE_VALUED_FUNCTION": "FUNCTION",
    "SQL_TRIGGER": "TRIGGER",
}


_TableKey = tuple[str, str]


def _scan_foreign_keys(conn: AzureSqlConnection) -> dict[_TableKey, list[ForeignKeyInfo]]:
    """FKs per table; the per-column rows (already in constraint-column order)
    are folded into one ForeignKeyInfo per constraint name."""
    fks: dict[_TableKey, dict[str, ForeignKeyInfo]] = {}
    for r in conn.query(_FOREIGN_KEYS_SQL):
        key = (r["TABLE_SCHEMA"], r["TABLE_NAME"])
        by_name = fks.setdefault(key, {})
        fk = by_name.get(r["FK_NAME"])
        if fk is None:
            fk = by_name[r["FK_NAME"]] = ForeignKeyInfo(
                name=r["FK_NAME"], columns=[],
                ref_schema=r["REF_SCHEMA"], ref_table=r["REF_TABLE"], ref_columns=[],
                on_delete=r["ON_DELETE"] or "NO_ACTION",
                on_update=r["ON_UPDATE"] or "NO_ACTION",
            )
        fk.columns.append(r["COLUMN_NAME"])
        fk.ref_columns.append(r["REF_COLUMN"])
    return {key: list(by_name.values()) for key, by_name in fks.items()}


def _scan_indexes(conn: AzureSqlConnection) -> dict[_TableKey, list[IndexInfo]]:
    """Indexes per table; key columns and INCLUDE columns folded per index name."""
    idx: dict[_TableKey, dict[str, IndexInfo]] = {}
    for r in conn.query(_INDEXES_SQL):
        key = (r["TABLE_SCHEMA"], r["TABLE_NAME"])
        by_name = idx.setdefault(key, {})
        ix = by_name.get(r["INDEX_NAME"])
        if ix is None:
            ix = by_name[r["INDEX_NAME"]] = IndexInfo(
                name=r["INDEX_NAME"], columns=[],
                is_unique=bool(r["IS_UNIQUE"]),
                filter_definition=r["FILTER_DEFINITION"] or None,
            )
        if r["IS_INCLUDED"]:
            ix.include_columns.append(r["COLUMN_NAME"])
        else:
            ix.columns.append(
                IndexColumnInfo(name=r["COLUMN_NAME"], descending=bool(r["IS_DESCENDING"]))
            )
    return {key: list(by_name.values()) for key, by_name in idx.items()}


def scan_tables(conn: AzureSqlConnection) -> list[TableInfo]:
    """Inventory user tables (columns, PKs, FKs, indexes, defaults, checks,
    identity, approximate row counts). Public so other phases (e.g.
    post-migration validation) can re-scan the source."""
    cols = conn.query(_COLUMNS_SQL)
    rowcounts = {
        (r["TABLE_SCHEMA"], r["TABLE_NAME"]): int(r["ROW_COUNT"] or 0)
        for r in conn.query(_ROWCOUNTS_SQL)
    }

    # PK columns per table, accumulated in ORDINAL_POSITION order (the query is
    # already sorted), so composite keys keep their column order.
    primary_keys: dict[_TableKey, list[str]] = {}
    for r in conn.query(_PRIMARY_KEYS_SQL):
        primary_keys.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append(r["COLUMN_NAME"])

    foreign_keys = _scan_foreign_keys(conn)
    indexes = _scan_indexes(conn)

    defaults: dict[_TableKey, list[ColumnDefaultInfo]] = {}
    for r in conn.query(_DEFAULTS_SQL):
        defaults.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append(
            ColumnDefaultInfo(column=r["COLUMN_NAME"], definition=r["DEFINITION"] or "")
        )

    checks: dict[_TableKey, list[CheckConstraintInfo]] = {}
    for r in conn.query(_CHECKS_SQL):
        checks.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append(
            CheckConstraintInfo(name=r["CHECK_NAME"], definition=r["DEFINITION"] or "")
        )

    identities: dict[_TableKey, str] = {
        (r["TABLE_SCHEMA"], r["TABLE_NAME"]): r["COLUMN_NAME"]
        for r in conn.query(_IDENTITY_SQL)
    }

    grouped: dict[_TableKey, list[ColumnInfo]] = {}
    for r in cols:
        key = (r["TABLE_SCHEMA"], r["TABLE_NAME"])
        grouped.setdefault(key, []).append(
            ColumnInfo(
                name=r["COLUMN_NAME"],
                data_type=r["DATA_TYPE"],
                max_length=r["CHARACTER_MAXIMUM_LENGTH"],
                precision=r["NUMERIC_PRECISION"],
                scale=r["NUMERIC_SCALE"],
                is_nullable=(r["IS_NULLABLE"] == "YES"),
                collation_name=r.get("COLLATION_NAME") or None,
            )
        )

    return [
        TableInfo(
            schema_name=schema,
            table_name=table,
            row_count=rowcounts.get((schema, table), 0),
            column_count=len(columns),
            columns=columns,
            primary_key=primary_keys.get((schema, table), []),
            identity_column=identities.get((schema, table)),
            foreign_keys=foreign_keys.get((schema, table), []),
            indexes=indexes.get((schema, table), []),
            column_defaults=defaults.get((schema, table), []),
            check_constraints=checks.get((schema, table), []),
        )
        for (schema, table), columns in sorted(grouped.items())
    ]


def scan_objects(conn: AzureSqlConnection) -> list[ProgrammableObject]:
    """Inventory user programmable objects (procs, views, functions, triggers)."""
    objects = []
    for r in conn.query(_MODULES_SQL):
        definition = r["DEFINITION"] or ""
        objects.append(
            ProgrammableObject(
                schema_name=r["SCHEMA_NAME"],
                object_name=r["OBJECT_NAME"],
                object_type=_TYPE_DESC_MAP.get(r["OBJECT_TYPE"], r["OBJECT_TYPE"]),
                line_count=definition.count("\n") + 1,
                definition=definition,
            )
        )
    return objects


def run_assessment(
    conn: AzureSqlConnection, *, use_ai: bool = True, endpoint: str | None = None
) -> AssessmentReport:
    """Full scan + compatibility analysis for a single Azure SQL database.

    The deterministic scan + rule findings always run. When ``use_ai`` is set, a
    Foundation Model deep-dive is layered on top (fail-soft — its failure never
    breaks the scan).
    """
    tables = scan_tables(conn)
    objects = scan_objects(conn)
    findings = compatibility.run_all_rules(tables, objects)
    rep = report.build_report(conn.database, tables, objects, findings)

    if use_ai:
        # Imported lazily so the scan has no hard dependency on the AI layer.
        from backend.assessment.ai_analysis import analyze_migration

        rep.ai_assessment = analyze_migration(rep, endpoint)

    return rep
