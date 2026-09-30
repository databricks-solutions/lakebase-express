"""T-SQL -> Postgres/Lakebase compatibility rule engine.

Four rule families:

  * **Type rules** run over scanned columns and flag SQL Server types that don't
    map 1:1 to Postgres (most are auto-handled and reported as INFO).
  * **Collation rules** run over character columns and flag collations whose
    comparison semantics need attention on the target — chiefly the
    case-insensitive ones, which are mirrored faithfully but at the cost of
    ``LIKE`` support (see schema_migration/collation_mapper).
  * **Code rules** are regex patterns run over the bodies of stored procedures,
    views, functions, and triggers. They surface the constructs that drive manual
    migration effort (cursors, dynamic SQL, T-SQL-only built-ins, etc.).
  * **Temp-object rules** are the one family a regex cannot answer: whether a
    `#temp` or `@t TABLE` becomes a CTE, a PL/pgSQL variable, or a real working
    table depends on how the body uses it, so the decision lives in
    assessment/temp_objects and this module only shapes it into findings.

Rules are data, not control flow — add a row, get a finding. The same severity
scale feeds the readiness score in report.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from backend.assessment import callable_shape
from backend.assessment.models import (
    Finding,
    ProgrammableObject,
    Severity,
    TableInfo,
)
from backend.assessment.temp_objects import (
    KIND_LABELS,
    KIND_RULE_IDS,
    REWRITES,
    STRATEGY_LABELS,
    WHY_NOT_TEMP,
    Strategy,
    TempUsage,
    analyze,
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
    # Findings for these two come from check_temp_objects, which reads how the body
    # uses each collection; the rows stay here so the rewrite table the context
    # bundle builds from CODE_RULES keeps its entry and its severity.
    CodeRule("TEMP_TABLE", "Temp table (#table)", Severity.MEDIUM, _rx(r"(?<!#)#[A-Za-z_]\w*"),
             "Rewrite as a CTE or a PL/pgSQL variable — NOT as a Postgres TEMP TABLE."),
    CodeRule("TABLE_VARIABLE", "Table variable (@table)", Severity.MEDIUM, _rx(r"DECLARE\s+@\w+\s+TABLE\b"),
             "Rewrite as a CTE or a PL/pgSQL variable — NOT as a Postgres TEMP TABLE."),
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


# Rules whose findings check_temp_objects emits instead — a regex can see that a
# scratch collection exists but not what should replace it.
_ANALYSED_ELSEWHERE = frozenset(KIND_RULE_IDS.values())


def check_code(objects: Iterable[ProgrammableObject]) -> list[Finding]:
    findings: list[Finding] = []
    for obj in objects:
        for rule in CODE_RULES:
            if rule.rule_id in _ANALYSED_ELSEWHERE:
                continue
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


# --- Temp tables and table variables ------------------------------------------------

# A collection that needs a real table is a decision someone has to make before the
# migration runs, not a mechanical rewrite — HIGH so it surfaces rather than sitting
# in the medium pile with the constructs the translator handles on its own.
_STRATEGY_SEVERITY = {
    Strategy.CTE: Severity.MEDIUM,
    Strategy.PLPGSQL: Severity.MEDIUM,
    Strategy.WORKING_TABLE: Severity.HIGH,
    Strategy.REVIEW: Severity.MEDIUM,
}


def check_temp_objects(
    tables: Iterable[TableInfo], objects: Iterable[ProgrammableObject]
) -> list[Finding]:
    """Findings for the `#temp` / `##temp` / `@t TABLE` collections in each body.

    Grouped per object per (kind, strategy) rather than one finding per collection:
    a reporting procedure with six scratch tables would otherwise contribute six
    findings saying the same thing and take 24 points off the readiness score over
    a single rewrite.

    Table row counts are passed through so a collection fed from a large table can
    escalate — it is the only way to tell "three rows in a variable" from "two
    million rows staged for a report".
    """
    row_counts = {t.fqn.lower(): t.row_count for t in tables}

    findings: list[Finding] = []
    for obj in objects:
        grouped: dict[tuple[str, Strategy], list[TempUsage]] = {}
        for usage in analyze(obj.definition, row_counts):
            grouped.setdefault((usage.obj.kind, usage.strategy), []).append(usage)

        for (kind, strategy), group in grouped.items():
            findings.append(
                Finding(
                    rule_id=KIND_RULE_IDS[kind],
                    title=f"{KIND_LABELS[kind]} → {STRATEGY_LABELS[strategy]}",
                    severity=_STRATEGY_SEVERITY[strategy],
                    object_name=f"{obj.schema_name}.{obj.object_name} ({obj.object_type})",
                    detail=f"{_evidence(group)}. {WHY_NOT_TEMP}",
                    recommendation=REWRITES[strategy],
                )
            )
    return findings


def _evidence(group: list[TempUsage]) -> str:
    """The collections in one finding and why they share a rewrite.

    Collapsed to `#a, #b — <reason>` when the reason is the same for all of them,
    which it usually is; only a mixed group pairs each name with its own.
    """
    reasons = {u.reason for u in group}
    names = ", ".join(u.obj.name for u in group)
    if len(reasons) == 1:
        return f"{names} — {reasons.pop()}"
    return "; ".join(f"{u.obj.name} ({u.reason})" for u in group)


# --- Callable shape ----------------------------------------------------------------


def check_result_set_procedures(
    objects: Iterable[ProgrammableObject],
) -> list[Finding]:
    """Procedures whose caller reads rows, so they must become functions.

    INFO, not a penalty: the translator does this reshaping on its own, and the
    readiness score is meant to measure manual effort. It is reported because the
    *call site* changes — `EXEC` becomes `SELECT * FROM`, never `CALL` — and an
    application migrated on the assumption that a procedure stays a procedure fails
    on every request with SQLSTATE 42809.
    """
    findings: list[Finding] = []
    for obj in objects:
        if obj.object_type.upper() != "PROCEDURE":
            continue
        if not callable_shape.returns_result_set(obj.definition):
            continue
        findings.append(
            Finding(
                rule_id="PROC_RETURNS_ROWS",
                title="Procedure returns a result set → becomes a FUNCTION",
                severity=Severity.INFO,
                object_name=f"{obj.schema_name}.{obj.object_name} ({obj.object_type})",
                detail=(
                    "This procedure ends with a SELECT, so its caller reads rows back. A "
                    "Postgres procedure cannot return a result set, so it is translated as "
                    "CREATE FUNCTION ... RETURNS TABLE instead."
                ),
                recommendation=(
                    "No schema work, but the call site changes: `EXEC` becomes "
                    "`SELECT * FROM <name>(...)`. Calling it with `CALL` fails with "
                    "SQLSTATE 42809."
                ),
            )
        )
    return findings


# --- Collation compatibility -------------------------------------------------------

# Operators Postgres refuses on a nondeterministic collation (NOT LIKE / NOT
# SIMILAR are covered by the LIKE/SIMILAR alternatives).
_PATTERN_MATCH = _rx(r"\b(like|similar\s+to)\b|~")


def check_collations(tables: Iterable[TableInfo]) -> list[Finding]:
    """Findings for column collations that change behaviour on the target.

    Grouped per table per collation, not per column: a wide table can have dozens
    of text columns sharing one collation, and a finding each would bury the rest
    of the report and tank the readiness score over a single fact.
    """
    from backend.schema_migration.collation_mapper import column_collation, parse_collation

    findings: list[Finding] = []
    for t in tables:
        insensitive: dict[str, list[str]] = {}
        unmapped: dict[str, list[str]] = {}
        fallback: dict[str, list[str]] = {}

        for col in t.columns:
            if not col.collation_name:
                continue
            target = column_collation(col)
            if target is None:
                # Report only a name that failed to parse — a non-character column
                # carrying a collation is not a problem.
                if parse_collation(col.collation_name) is None:
                    unmapped.setdefault(col.collation_name, []).append(col.name)
                continue
            if not target.deterministic:
                insensitive.setdefault(col.collation_name, []).append(col.name)
            if target.locale_fallback:
                fallback.setdefault(col.collation_name, []).append(col.name)

        for name, cols in sorted(insensitive.items()):
            findings.append(Finding(
                rule_id="COLLATION_INSENSITIVE",
                title=f"Case/accent-insensitive collation '{name}'",
                severity=Severity.MEDIUM,
                object_name=f"{t.fqn} ({', '.join(sorted(cols))})",
                detail=(
                    f"{name} compares strings case- and/or accent-insensitively, so the "
                    "migration mirrors it with a nondeterministic Postgres ICU collation — "
                    "without it, equality, ORDER BY, GROUP BY and unique indexes would all "
                    "turn case-sensitive on the target. PostgreSQL does not allow LIKE or "
                    "regex pattern matching against a nondeterministic collation."
                ),
                recommendation=(
                    "Keep the mirrored collation for identical comparison semantics, and "
                    "check whether queries LIKE/pattern-match these columns. Those need an "
                    'explicit deterministic collation on the operand — col COLLATE "C" '
                    "ILIKE '...' keeps the case-insensitive result. Note that lower(col) "
                    "alone is NOT enough: the result of lower() inherits the column's "
                    'collation, so it is still rejected (lower(col) COLLATE "C" works).'
                ),
            ))

        for name, cols in sorted(fallback.items()):
            findings.append(Finding(
                rule_id="COLLATION_LOCALE_FALLBACK",
                title=f"Unrecognised collation locale '{name}'",
                severity=Severity.LOW,
                object_name=f"{t.fqn} ({', '.join(sorted(cols))})",
                detail=(
                    f"The comparison strength of {name} is mirrored, but its locale has no "
                    "known ICU equivalent, so the ICU root locale is used instead. "
                    "Case/accent behaviour matches; language-specific sort order may not."
                ),
                recommendation=(
                    "Confirm the sort order suits this data, or edit the generated "
                    "CREATE COLLATION to the correct ICU locale in the plan."
                ),
            ))

        # Postgres rejects pattern matching on a nondeterministic collation
        # outright, so these objects are certain to fail in the post-data phase —
        # HIGH, and reported here rather than discovered at apply time.
        ci_columns = {c for cols in insensitive.values() for c in cols}
        if ci_columns:
            for obj_name, predicate in [
                *((f"CHECK {chk.name}", chk.definition) for chk in t.check_constraints),
                *((f"index {ix.name}", ix.filter_definition or "")
                  for ix in t.indexes if ix.filter_definition),
            ]:
                if not _PATTERN_MATCH.search(predicate):
                    continue
                hit = sorted(c for c in ci_columns if c.lower() in predicate.lower())
                if not hit:
                    continue
                findings.append(Finding(
                    rule_id="COLLATION_PATTERN_MATCH",
                    title="Pattern match on a case-insensitive column",
                    severity=Severity.HIGH,
                    object_name=f"{t.fqn} · {obj_name}",
                    detail=(
                        f"{obj_name} pattern-matches {', '.join(hit)}, whose collation is "
                        "case/accent-insensitive and therefore nondeterministic in Postgres. "
                        "PostgreSQL rejects LIKE and regex matching against a nondeterministic "
                        f"collation, so this object will fail to apply. Predicate: {predicate}"
                    ),
                    recommendation=(
                        'Force a deterministic collation on the operand: col COLLATE "C" '
                        "ILIKE '...' preserves the source's case-insensitive result "
                        '(plain LIKE after COLLATE "C" becomes case-SENSITIVE). '
                        "lower(col) on its own does not help — its result keeps the "
                        "column's collation and is rejected just the same."
                    ),
                ))

        for name, cols in sorted(unmapped.items()):
            findings.append(Finding(
                rule_id="COLLATION_UNMAPPED",
                title=f"Collation '{name}' not translated",
                severity=Severity.LOW,
                object_name=f"{t.fqn} ({', '.join(sorted(cols))})",
                detail=(
                    f"{name} could not be parsed, so these columns are created with the "
                    "target database's default collation — which is case-sensitive, unlike "
                    "most SQL Server collations."
                ),
                recommendation=(
                    "Add an explicit COLLATE to these columns in the plan if their "
                    "comparison semantics matter."
                ),
            ))
    return findings


def run_all_rules(
    tables: Iterable[TableInfo], objects: Iterable[ProgrammableObject]
) -> list[Finding]:
    # Materialised: every family below walks both sequences, so a generator would
    # arrive empty at the second one.
    tables, objects = list(tables), list(objects)
    return (
        check_types(tables)
        + check_collations(tables)
        + check_code(objects)
        + check_temp_objects(tables, objects)
        + check_result_set_procedures(objects)
    )
