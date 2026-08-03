"""T-SQL -> Postgres/Lakebase compatibility rule engine.

Two rule families:

  * **Type rules** run over scanned columns and flag SQL Server types that don't
    map 1:1 to Postgres (most are auto-handled and reported as INFO).
  * **Code rules** are regex patterns run over the bodies of stored procedures,
    views, functions, and triggers. They surface the constructs that drive manual
    migration effort (cursors, dynamic SQL, T-SQL-only built-ins, etc.).

Rules are data, not control flow — add a row, get a finding. The same severity
scale feeds the readiness score in report.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from backend.assessment.models import (
    Finding,
    ProgrammableObject,
    Severity,
    TableInfo,
)

# --- Type compatibility ----------------------------------------------------------

# Types Postgres has no direct analog for; the value is the recommended target
# and the severity of the conversion. Types not listed here are assumed to map
# cleanly (handled in schema_migration.type_mapper in a later phase).
_TYPE_NOTES: dict[str, tuple[str, Severity, str]] = {
    "datetime": ("timestamp(3)", Severity.LOW, "datetime is ~3.33ms precision and timezone-naive; map to timestamp(3) and decide timestamp vs timestamptz."),
    "datetime2": ("timestamp", Severity.INFO, "Direct map to timestamp."),
    "datetimeoffset": ("timestamptz", Severity.LOW, "Map to timestamptz; verify offset handling."),
    "smalldatetime": ("timestamp(0)", Severity.LOW, "Minute precision; map to timestamp(0)."),
    "money": ("numeric(19,4)", Severity.LOW, "No money type semantics in PG; use numeric(19,4)."),
    "smallmoney": ("numeric(10,4)", Severity.LOW, "Use numeric(10,4)."),
    "uniqueidentifier": ("uuid", Severity.INFO, "Map to uuid; GUID byte order/casing must be normalised on load."),
    "bit": ("boolean", Severity.INFO, "Map to boolean (0/1 -> false/true)."),
    "tinyint": ("smallint", Severity.INFO, "No 1-byte int in PG; widen to smallint."),
    # Fixed-length character types: SQL Server blank-pads CHAR/NCHAR to the
    # declared length; that trailing-space semantics is lost when mapping to text.
    "char": ("varchar(n)", Severity.LOW, "CHAR is blank-padded to a fixed length; map to varchar(n)/text and verify trailing-space comparisons."),
    "nchar": ("varchar(n)", Severity.LOW, "NCHAR is blank-padded to a fixed length; map to varchar(n)/text and verify trailing-space comparisons."),
    # Approximate numerics: round-trip precision and locale formatting need care.
    "real": ("real", Severity.INFO, "Approximate float; verify precision/rounding on conversion."),
    "float": ("double precision", Severity.INFO, "Approximate float; map to double precision and verify precision/rounding."),
    "image": ("bytea", Severity.MEDIUM, "Deprecated LOB; migrate to bytea."),
    "text": ("text", Severity.LOW, "Deprecated LOB; map to text."),
    "ntext": ("text", Severity.LOW, "Deprecated LOB; map to text."),
    "varbinary": ("bytea", Severity.LOW, "Map to bytea."),
    "binary": ("bytea", Severity.LOW, "Map to bytea."),
    "hierarchyid": ("text/ltree", Severity.HIGH, "No PG equivalent; redesign required."),
    "geography": ("PostGIS geography", Severity.HIGH, "Requires PostGIS; not available in base Lakebase."),
    "geometry": ("PostGIS geometry", Severity.HIGH, "Requires PostGIS; not available in base Lakebase."),
    "sql_variant": ("text/jsonb", Severity.HIGH, "No PG equivalent; redesign required."),
    "xml": ("xml", Severity.MEDIUM, "PG xml type is limited vs T-SQL XML methods."),
    "rowversion": ("bytea", Severity.MEDIUM, "No auto-versioning; replace with trigger or app logic."),
    "timestamp": ("bytea", Severity.MEDIUM, "SQL Server 'timestamp' is rowversion, NOT a datetime."),
}


def check_types(tables: Iterable[TableInfo]) -> list[Finding]:
    findings: list[Finding] = []
    for t in tables:
        for col in t.columns:
            note = _TYPE_NOTES.get(col.data_type.lower())
            if not note:
                continue
            target, severity, detail = note
            findings.append(
                Finding(
                    rule_id=f"TYPE_{col.data_type.upper()}",
                    title=f"Type '{col.data_type}' needs mapping",
                    severity=severity,
                    object_name=f"{t.fqn}.{col.name}",
                    detail=detail,
                    recommendation=f"Map to Postgres '{target}'.",
                )
            )
    return findings


# --- Code compatibility ----------------------------------------------------------


@dataclass(frozen=True)
class CodeRule:
    rule_id: str
    title: str
    severity: Severity
    pattern: re.Pattern[str]
    recommendation: str
    # Optional object-type filter (e.g. only meaningful inside triggers).
    applies_to: tuple[str, ...] | None = None


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


CODE_RULES: list[CodeRule] = [
    CodeRule("CURSOR", "Cursor usage", Severity.HIGH, _rx(r"\bDECLARE\s+\w+\s+CURSOR\b"),
             "Rewrite as set-based SQL or a PL/pgSQL loop; cursors rarely port cleanly."),
    CodeRule("DYNAMIC_SQL", "Dynamic SQL (EXEC/sp_executesql)", Severity.HIGH,
             _rx(r"\b(sp_executesql|EXEC\s*\(|EXECUTE\s*\()"),
             "Reimplement with PL/pgSQL EXECUTE ... USING and review for injection."),
    CodeRule("TEMP_TABLE", "Temp table (#table)", Severity.MEDIUM, _rx(r"#\w+"),
             "Use CREATE TEMP TABLE or a CTE in Postgres."),
    CodeRule("TABLE_VARIABLE", "Table variable (@table)", Severity.MEDIUM, _rx(r"DECLARE\s+@\w+\s+TABLE\b"),
             "Replace with a TEMP TABLE or array/CTE."),
    CodeRule("MERGE", "MERGE statement", Severity.MEDIUM, _rx(r"\bMERGE\s+INTO\b|\bMERGE\s+\w+\s+USING\b"),
             "PG 15+ supports MERGE; otherwise use INSERT ... ON CONFLICT."),
    CodeRule("TOP", "TOP clause", Severity.LOW, _rx(r"\bSELECT\s+TOP\b"),
             "Replace with LIMIT."),
    CodeRule("ISNULL", "ISNULL()", Severity.LOW, _rx(r"\bISNULL\s*\("),
             "Replace with COALESCE()."),
    CodeRule("GETDATE", "GETDATE()/SYSDATETIME()", Severity.LOW, _rx(r"\b(GETDATE|SYSDATETIME|GETUTCDATE)\s*\("),
             "Replace with now() / CURRENT_TIMESTAMP."),
    CodeRule("IDENTITY", "IDENTITY column / @@IDENTITY", Severity.MEDIUM,
             _rx(r"\bIDENTITY\s*\(|@@IDENTITY|SCOPE_IDENTITY"),
             "Use GENERATED ... AS IDENTITY and RETURNING for last id."),
    CodeRule("STRING_FUNCS", "T-SQL string funcs (LEN/CHARINDEX/...)", Severity.LOW,
             _rx(r"\b(LEN|CHARINDEX|DATEPART|DATEADD|DATEDIFF|STUFF|PATINDEX)\s*\("),
             "Map to PG equivalents (length, position, date_part, etc.)."),
    CodeRule("SQUARE_BRACKETS", "[bracketed] identifiers", Severity.INFO, _rx(r"\[[^\]]+\]"),
             'Replace [name] with "name" (double quotes) for Postgres.'),
    CodeRule("PLUS_CONCAT", "'+' string concatenation", Severity.LOW, _rx(r"'\s*\+|\+\s*'"),
             "Use || or concat() in Postgres."),
    CodeRule("TRY_CATCH", "TRY/CATCH error handling", Severity.MEDIUM, _rx(r"\bBEGIN\s+TRY\b"),
             "Reimplement with BEGIN ... EXCEPTION WHEN in PL/pgSQL."),
    CodeRule("INSERTED_DELETED", "INSERTED/DELETED pseudo-tables", Severity.HIGH,
             _rx(r"\b(INSERTED|DELETED)\b"), applies_to=("TRIGGER",),
             recommendation="Use NEW/OLD records in PL/pgSQL row-level triggers."),
    CodeRule("LINKED_SERVER", "Linked-server / 4-part name", Severity.HIGH,
             _rx(r"\b\w+\.\w+\.\w+\.\w+\b"),
             "No linked servers; use FDW or stage data separately."),
    CodeRule("RAISERROR", "RAISERROR / THROW", Severity.MEDIUM, _rx(r"\b(RAISERROR|THROW)\b"),
             "Reimplement with RAISE [EXCEPTION] in PL/pgSQL."),
    CodeRule("OUTPUT_CLAUSE", "OUTPUT INSERTED/DELETED clause", Severity.MEDIUM,
             _rx(r"\bOUTPUT\s+(INSERTED|DELETED)\b"),
             "Use a RETURNING clause in Postgres."),
    CodeRule("NEWID", "NEWID()/NEWSEQUENTIALID()", Severity.LOW, _rx(r"\bNEW(SEQUENTIAL)?ID\s*\("),
             "Replace with gen_random_uuid() (pgcrypto) or uuid_generate_v4()."),
    CodeRule("ROWCOUNT", "@@ROWCOUNT", Severity.LOW, _rx(r"@@ROWCOUNT"),
             "Use GET DIAGNOSTICS n = ROW_COUNT in PL/pgSQL."),
    CodeRule("IIF_CHOOSE", "IIF()/CHOOSE()", Severity.LOW, _rx(r"\b(IIF|CHOOSE)\s*\("),
             "Rewrite with a CASE expression."),
    CodeRule("COLLATE", "Explicit COLLATE clause", Severity.MEDIUM, _rx(r"\bCOLLATE\b"),
             "Map to a Postgres collation; case-insensitive collations need citext or lower()."),
    CodeRule("MAX_LOB", "(MAX) large-object type", Severity.LOW,
             _rx(r"\b(n?varchar|varbinary)\s*\(\s*max\s*\)"),
             "varchar(max)/varbinary(max) → text/bytea in Postgres."),
    CodeRule("SET_OPTIONS", "T-SQL session SET options", Severity.INFO,
             _rx(r"\bSET\s+(NOCOUNT|ANSI_NULLS|QUOTED_IDENTIFIER|XACT_ABORT|ANSI_PADDING)\b"),
             "No Postgres equivalent; remove these session SET statements."),
]


def check_code(objects: Iterable[ProgrammableObject]) -> list[Finding]:
    findings: list[Finding] = []
    for obj in objects:
        for rule in CODE_RULES:
            if rule.applies_to and obj.object_type.upper() not in rule.applies_to:
                continue
            if rule.pattern.search(obj.definition):
                findings.append(
                    Finding(
                        rule_id=rule.rule_id,
                        title=rule.title,
                        severity=rule.severity,
                        object_name=f"{obj.schema_name}.{obj.object_name} ({obj.object_type})",
                        detail=f"Pattern matched in {obj.object_type.lower()} body ({obj.line_count} lines).",
                        recommendation=rule.recommendation,
                    )
                )
    return findings


def run_all_rules(
    tables: Iterable[TableInfo], objects: Iterable[ProgrammableObject]
) -> list[Finding]:
    return check_types(tables) + check_code(objects)
