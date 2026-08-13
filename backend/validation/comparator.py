"""Compares the source database with the Lakebase target after a migration.

Three stages, kept separable for testability:

  * source inventory  — reuses the assessment scanner (user objects only);
  * target inventory  — Postgres catalog queries, restricted to the schemas the
    migration maps into (so pre-existing unrelated schemas are never flagged);
  * ``compare``       — a pure function that lines both sides up through the
    naming rules (``schema_migration/naming.py``) and emits ValidationItems.

Row counts are exact (``COUNT(*)`` on both sides) for tables that exist on both
sides — the assessment's approximate partition-stats counts are not trustworthy
enough to declare a migration consistent. Large tables therefore take a while;
the run reports per-table progress. Above ``_EXACT_COUNT_MAX_ROWS`` an exact
scan risks the target's server-side statement timeout, so those tables fall back
to planner estimates on both sides (compared with a small tolerance and flagged
as approximate) rather than being left uncounted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from backend.assessment.models import ProgrammableObject, Severity, TableInfo
from backend.assessment.scanner import scan_objects, scan_tables
from backend.connectors.lakebase import LakebaseConnection
from backend.migration.models import KIND_ORDER, ObjectKind
from backend.schema_migration.collation_mapper import column_collation
from backend.schema_migration.ddl_generator import (
    check_constraint_ddl,
    column_default_ddl,
    foreign_key_ddl,
    identity_ddl,
    index_ddl,
    primary_key_ddl,
    schema_ddl,
    table_ddl,
)
from backend.schema_migration.naming import (
    IdentifierCase,
    index_name,
    map_object,
    map_schema,
    mapped_identifier,
    primary_key_name,
    trigger_function_name,
)
from backend.schema_migration.type_mapper import map_type
from backend.validation.models import (
    MatchStatus,
    ObjectDiff,
    ValidationItem,
    ValidationReport,
)

log = logging.getLogger("lakebase_express.validation")

# Severity ranking (the assessment's penalty scale) — used to pick the worst
# severity among a table's findings.
_PENALTY = {Severity.INFO: 0, Severity.LOW: 1, Severity.MEDIUM: 4, Severity.HIGH: 10}

_OBJECT_TYPE_TO_KIND = {
    "PROCEDURE": ObjectKind.PROCEDURE,
    "VIEW": ObjectKind.VIEW,
    "FUNCTION": ObjectKind.FUNCTION,
    "TRIGGER": ObjectKind.TRIGGER,
}

# Progress callback: (phase, done, total, current-object).
ProgressFn = Callable[[str, int, int, str], None]


# --- Target (Postgres) inventory ---------------------------------------------------

_PG_SCHEMAS_SQL = "SELECT schema_name AS name FROM information_schema.schemata"

_PG_TABLES_SQL = """
SELECT table_schema AS schema, table_name AS name
FROM   information_schema.tables
WHERE  table_type = 'BASE TABLE' AND table_schema = ANY(%(schemas)s)
"""

_PG_VIEWS_SQL = """
SELECT table_schema AS schema, table_name AS name
FROM   information_schema.views
WHERE  table_schema = ANY(%(schemas)s)
"""

# Two classes of function are platform plumbing, not migrated user code, and are
# excluded so they don't show up as "extra in target":
#   * extension-owned routines (pg_depend deptype 'e') — e.g. every pgcrypto/…
#     function that lives in `public`;
#   * event-trigger functions (backing a row in pg_event_trigger) — Lakebase
#     provisions helpers like public.grant_usage_on_new_schema to auto-grant
#     privileges on newly created objects; they aren't tied to an extension, so
#     the deptype filter alone doesn't catch them.
_PG_ROUTINES_SQL = """
SELECT    n.nspname AS schema, p.proname AS name,
          CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind
FROM      pg_proc p
JOIN      pg_namespace n ON n.oid = p.pronamespace
LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
WHERE     n.nspname = ANY(%(schemas)s)
  AND     p.prokind IN ('f', 'p')
  AND     d.objid IS NULL
  AND     NOT EXISTS (SELECT 1 FROM pg_event_trigger et WHERE et.evtfoid = p.oid)
"""

_PG_TRIGGERS_SQL = """
SELECT DISTINCT trigger_schema AS schema, trigger_name AS name
FROM   information_schema.triggers
WHERE  trigger_schema = ANY(%(schemas)s)
"""

# collation_name is the column's *explicit* collation — NULL when it inherits the
# database default, which is exactly the "no COLLATE applied" case to detect.
_PG_COLUMNS_SQL = """
SELECT table_schema AS schema, table_name AS "table", column_name AS name, data_type,
       column_default IS NOT NULL AS has_default,
       is_identity = 'YES' AS is_identity,
       collation_name
FROM   information_schema.columns
WHERE  table_schema = ANY(%(schemas)s)
"""

# Table constraints, by class: 'p' primary key, 'c' check, 'f' foreign key.
# Unique constraints are deliberately absent — the scanner reads source unique
# constraints as unique *indexes* (assessment/scanner._INDEXES_SQL), and the
# migration recreates them as indexes, so they are compared as indexes too.
#
# NOT NULL arrives as a column attribute rather than a pg_constraint row, so it
# is compared as part of the table's column structure, not here.
_PG_CONSTRAINTS_SQL = """
SELECT n.nspname AS schema, t.relname AS "table", c.conname AS name, c.contype AS kind,
       ARRAY(
           SELECT a.attname
           FROM   unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN   pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
           ORDER  BY k.ord
       ) AS columns
FROM   pg_constraint c
JOIN   pg_class t ON t.oid = c.conrelid
JOIN   pg_namespace n ON n.oid = t.relnamespace
WHERE  n.nspname = ANY(%(schemas)s)
  AND  c.contype IN ('p', 'c', 'f')
"""

# Indexes, excluding those Postgres creates to back a PK or UNIQUE *constraint*
# (conindid) — those are not separate objects, and counting them would report a
# phantom "extra index" on every keyed table. A unique index created directly by
# the migration has no backing constraint, so it is kept.
#
# Only KEY columns are collected: ``ord <= indnkeyatts`` drops the trailing
# INCLUDE columns, which are payload rather than part of the index key (and so
# are not compared). Filtering on ordinality rather than slicing ``indkey``
# avoids that column's 0-based subscripting — ``conkey`` above is an ordinary
# 1-based array, and mixing the two conventions is an easy off-by-one.
# Expression indexes carry attnum 0, which joins to no column and drops out; the
# migration does not generate them.
_PG_INDEXES_SQL = """
SELECT n.nspname AS schema, t.relname AS "table", ix.relname AS name, i.indisunique AS is_unique,
       ARRAY(
           SELECT a.attname
           FROM   unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN   pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
           WHERE  k.ord <= i.indnkeyatts
           ORDER  BY k.ord
       ) AS columns
FROM   pg_index i
JOIN   pg_class ix ON ix.oid = i.indexrelid
JOIN   pg_class t ON t.oid = i.indrelid
JOIN   pg_namespace n ON n.oid = t.relnamespace
WHERE  n.nspname = ANY(%(schemas)s)
  AND  NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
"""


@dataclass
class TargetObject:
    """A target constraint or index, with the columns it covers.

    Columns are compared where they are genuinely comparable (keys and index
    columns). Check predicates and default expressions are NOT: the source
    stores ``([Qty]>(0))`` and Postgres stores ``(qty > 0)``, so a text
    comparison would report a mismatch on every correctly-migrated constraint.
    Those are compared by existence, and the source expression is shown in the
    report so a human can eyeball it.
    """

    name: str
    columns: tuple[str, ...] = ()
    is_unique: bool = False


@dataclass
class TargetInventory:
    """What actually exists in the target, within the migration's schemas."""

    schemas: set[str] = field(default_factory=set)
    tables: set[tuple[str, str]] = field(default_factory=set)
    views: set[tuple[str, str]] = field(default_factory=set)
    procedures: set[tuple[str, str]] = field(default_factory=set)
    functions: set[tuple[str, str]] = field(default_factory=set)
    triggers: set[tuple[str, str]] = field(default_factory=set)
    # (schema, table) -> {column name: information_schema data_type}
    columns: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # (schema, table) -> {column: explicit collation}. Columns on the database
    # default have no entry — that absence is how a dropped collation is spotted.
    column_collations: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)

    # Whether the post-data catalogs below were actually queried. An empty dict
    # is ambiguous on its own — it means both "this target has no constraints"
    # and "nobody looked" — and guessing wrong would report every constraint of a
    # correctly-migrated database as missing. Only ``fetch_target_inventory``
    # sets this; an inventory built by hand (structure-only compares, tests)
    # leaves it False and those kinds are simply not compared.
    post_data_scanned: bool = False

    # Post-data objects, keyed by target (schema, table).
    primary_keys: dict[tuple[str, str], TargetObject] = field(default_factory=dict)
    checks: dict[tuple[str, str], list[TargetObject]] = field(default_factory=dict)
    foreign_keys: dict[tuple[str, str], list[TargetObject]] = field(default_factory=dict)
    indexes: dict[tuple[str, str], list[TargetObject]] = field(default_factory=dict)
    # Columns carrying a DEFAULT, and columns that are Postgres identity columns.
    column_defaults: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    identity_columns: dict[tuple[str, str], set[str]] = field(default_factory=dict)


def fetch_target_inventory(conn: LakebaseConnection, schemas: list[str]) -> TargetInventory:
    params = {"schemas": schemas}
    inv = TargetInventory(
        schemas={r["name"] for r in conn.query(_PG_SCHEMAS_SQL)},
        tables={(r["schema"], r["name"]) for r in conn.query(_PG_TABLES_SQL, params)},
        views={(r["schema"], r["name"]) for r in conn.query(_PG_VIEWS_SQL, params)},
        triggers={(r["schema"], r["name"]) for r in conn.query(_PG_TRIGGERS_SQL, params)},
        post_data_scanned=True,
    )
    for r in conn.query(_PG_ROUTINES_SQL, params):
        bucket = inv.procedures if r["kind"] == "procedure" else inv.functions
        bucket.add((r["schema"], r["name"]))
    for r in conn.query(_PG_COLUMNS_SQL, params):
        key = (r["schema"], r["table"])
        inv.columns.setdefault(key, {})[r["name"]] = r["data_type"]
        if r.get("collation_name"):
            inv.column_collations.setdefault(key, {})[r["name"]] = r["collation_name"]
        if r["has_default"]:
            inv.column_defaults.setdefault(key, set()).add(r["name"])
        if r["is_identity"]:
            inv.identity_columns.setdefault(key, set()).add(r["name"])
    for r in conn.query(_PG_CONSTRAINTS_SQL, params):
        key = (r["schema"], r["table"])
        obj = TargetObject(name=r["name"], columns=tuple(r["columns"] or ()))
        if r["kind"] == "p":
            inv.primary_keys[key] = obj
        elif r["kind"] == "c":
            inv.checks.setdefault(key, []).append(obj)
        else:
            inv.foreign_keys.setdefault(key, []).append(obj)
    for r in conn.query(_PG_INDEXES_SQL, params):
        inv.indexes.setdefault((r["schema"], r["table"]), []).append(
            TargetObject(name=r["name"], columns=tuple(r["columns"] or ()),
                         is_unique=bool(r["is_unique"]))
        )
    return inv


# --- Exact row counts ---------------------------------------------------------------


def _tsql_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def count_source_rows(source, schema: str, table: str, timeout: int | None = None) -> int:
    rows = source.query(
        f"SELECT COUNT_BIG(*) AS n FROM {_tsql_ident(schema)}.{_tsql_ident(table)}",
        timeout=timeout,
    )
    return int(rows[0]["n"] or 0)


def count_target_rows(
    target: LakebaseConnection, schema: str, table: str, timeout: int | None = None
) -> int:
    rows = target.query(
        f"SELECT count(*) AS n FROM {_pg_ident(schema)}.{_pg_ident(table)}",
        statement_timeout_ms=(timeout * 1000 if timeout else None),
    )
    return int(rows[0]["n"] or 0)


# In the default (estimate) mode, above this source estimate we skip the exact
# COUNT(*) on both sides: on a managed Lakebase instance a full scan of hundreds
# of millions of rows can exceed the server-side statement timeout, which used
# to strand the table with a blank target count. Estimates are instant and,
# after a bulk load, plenty accurate to confirm parity. When the user opts into
# exact counts this threshold is ignored — every table is counted for real.
_EXACT_COUNT_MAX_ROWS = 5_000_000

# Estimated counts (partition stats / reltuples) drift slightly from the true
# value, so require exact equality only for exact counts and allow this relative
# slack when either side is an estimate.
_ESTIMATE_TOLERANCE = 0.005

# Per-query timeout for exact counts (seconds). Generous so a full scan of a
# very large table can finish; the whole point of opting out of estimates is
# precision, so we wait for it rather than cut the scan short.
_EXACT_COUNT_TIMEOUT_SECONDS = 600


def estimate_target_rows(target: LakebaseConnection, schema: str, table: str) -> int:
    """Planner's row estimate for a table (``pg_class.reltuples``), no table scan.

    Runs a sampling ``ANALYZE`` first so the estimate reflects the freshly
    loaded data — reltuples is otherwise whatever the last autovacuum saw, which
    for a just-migrated table can still be 0. Returns 0 if the estimate is
    unavailable (e.g. never analyzed).
    """
    target.execute(f"ANALYZE {_pg_ident(schema)}.{_pg_ident(table)}")
    rows = target.query(
        "SELECT reltuples::bigint AS n FROM pg_class c "
        "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
        "WHERE ns.nspname = %(schema)s AND c.relname = %(table)s",
        {"schema": schema, "table": table},
    )
    return int(rows[0]["n"]) if rows and rows[0]["n"] is not None and rows[0]["n"] >= 0 else 0


# --- Comparison (pure) --------------------------------------------------------------

# information_schema spells out the long form of some type names; normalize the
# type-mapper output the same way so equal types compare equal.
_PG_TYPE_ALIASES = {
    "varchar": "character varying",
    "char": "character",
    "timestamp": "timestamp without time zone",
    "timestamptz": "timestamp with time zone",
    "time": "time without time zone",
}


def _expected_pg_type(col) -> str:
    base = map_type(col).split("(", 1)[0].strip().lower()
    return _PG_TYPE_ALIASES.get(base, base)


def _collation_drift(t: TableInfo, target_collations: dict[str, str], target_cols: dict) -> list[str]:
    """Columns whose target collation isn't the one the source collation maps to.

    Invisible to every other check: the column holds exactly the right bytes and
    passes type and row-count comparison while comparing them differently —
    ``'ana' = 'ANA'`` flips, sort order changes, unique indexes accept pairs the
    source rejected. A source column with no (or untranslatable) collation makes no
    claim about the target and is skipped.
    """
    drift: list[str] = []
    for c in t.columns:
        if c.name not in target_cols:
            continue  # already reported as a missing column
        expected = column_collation(c)
        if expected is None:
            continue
        found = target_collations.get(c.name)
        if found == expected.name:
            continue
        drift.append(
            f"{c.name}: expected {expected.name} "
            f"(source {c.collation_name}), found {found or 'the database default'}"
        )
    return drift


def _recollate_sql(
    t: TableInfo, schema: str, table: str, collation_schema: str,
    target_collations: dict[str, str],
) -> str:
    """SQL that puts the source collation back on the drifted columns.

    The CREATE is included (guarded) because the usual reason a column lacks a
    collation is that step never ran. ALTER COLUMN TYPE is how an existing column
    gets a collation; it rewrites and reindexes the table, which the emitted SQL
    notes since that isn't free on a large table.
    """
    creates: dict[str, str] = {}
    alters: list[str] = []
    for c in t.columns:
        expected = column_collation(c)
        if expected is None or target_collations.get(c.name) == expected.name:
            continue
        ddl = expected.ddl(collation_schema)
        if ddl:
            creates.setdefault(expected.name, ddl)
        alters.append(
            f"ALTER TABLE {_pg_ident(schema)}.{_pg_ident(table)} "
            f'ALTER COLUMN "{c.name}" TYPE {map_type(c)} '
            f"COLLATE {expected.qualified(collation_schema)};"
        )
    if not alters:
        return ""
    header = (
        "-- Restores the source collation on these columns. ALTER COLUMN ... TYPE\n"
        "-- rewrites the table and rebuilds its indexes — on a large table, plan for it."
    )
    return "\n".join([header, *creates.values(), *alters])


def _add_column_sql(schema: str, table: str, col, collation_schema: str = "") -> str:
    """ADD COLUMN for a column missing from the target, carrying its source
    collation like the original CREATE TABLE does. Re-added without it the column
    takes the case-sensitive default, and being *missing* rather than drifted, no
    collation finding would flag it afterwards."""
    null = "" if col.is_nullable else " NOT NULL"
    target = column_collation(col)
    collate = f" COLLATE {target.qualified(collation_schema)}" if target else ""
    return (
        f"ALTER TABLE {_pg_ident(schema)}.{_pg_ident(table)} "
        f'ADD COLUMN "{col.name}" {map_type(col)}{collate}{null};'
    )


def _drop_sql(kind: ObjectKind, schema: str, name: str) -> str:
    keyword = {"table": "TABLE", "view": "VIEW", "procedure": "PROCEDURE",
               "function": "FUNCTION"}.get(kind.value)
    if not keyword:
        return ""
    return (
        "-- Destructive: only run this if the object should not exist in Lakebase.\n"
        f"DROP {keyword} IF EXISTS {_pg_ident(schema)}.{_pg_ident(name)};"
    )


def _sentence(text: str) -> str:
    """Upper-case only the first character (str.capitalize would fold column names)."""
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _cols(names) -> str:
    return ", ".join(names) if names else "—"


def _post_data_rollup(
    t: TableInfo,
    inv: TargetInventory,
    mapped: tuple[str, str],
    *,
    kind: ObjectKind,
    expected: list[tuple[str, str, str, tuple[str, ...] | None]],
    present: list[TargetObject],
    fix_for: dict[str, str],
    label: str,
    recommendation: str,
) -> ValidationItem | None:
    """Compare one table's objects of a single post-data kind into one item.

    ``expected`` is (target name, display name, source definition, columns to
    compare or None to compare existence only) per source object; ``present``
    is what the target has. ``fix_for`` maps a target name to the DDL that
    creates it, so the item's ``fix_sql`` recreates exactly what is missing.

    Returns None when the source table has none of this kind AND the target has
    none either — there is nothing to report, and an empty "0 of 0" row on every
    table would be pure noise.
    """
    by_name = {p.name: p for p in present}
    diffs: list[ObjectDiff] = []
    fixes: list[str] = []
    matched = 0

    for target_name, display, definition, columns in expected:
        found = by_name.get(target_name)
        if found is None:
            diffs.append(ObjectDiff(
                name=display, status=MatchStatus.MISSING,
                detail=f'not found in the target (expected "{target_name}")',
                source_definition=definition,
            ))
            if fix_for.get(target_name):
                fixes.append(fix_for[target_name])
            continue
        # Columns are compared only where both sides are genuinely comparable
        # (keys, index columns). Predicates and default expressions are compared
        # by existence — see TargetObject.
        if columns is not None and found.columns and tuple(columns) != found.columns:
            diffs.append(ObjectDiff(
                name=display, status=MatchStatus.MISMATCH,
                detail=f"columns differ — source ({_cols(columns)}), "
                       f"target ({_cols(found.columns)})",
                source_definition=definition,
            ))
            continue
        matched += 1
        diffs.append(ObjectDiff(name=display, status=MatchStatus.MATCHED,
                                source_definition=definition))

    claimed = {name for name, _, _, _ in expected}
    for p in present:
        if p.name in claimed:
            continue
        diffs.append(ObjectDiff(
            name=p.name, status=MatchStatus.EXTRA,
            detail="exists in Lakebase but not in the source",
        ))

    if not expected and not present:
        return None

    missing = sum(d.status is MatchStatus.MISSING for d in diffs)
    mismatched = sum(d.status is MatchStatus.MISMATCH for d in diffs)
    extra = sum(d.status is MatchStatus.EXTRA for d in diffs)

    problems: list[str] = []
    if missing:
        problems.append(f"{missing} missing in the target")
    if mismatched:
        problems.append(f"{mismatched} with different columns")
    if extra:
        problems.append(f"{extra} only in the target")

    if missing or mismatched:
        status, severity = MatchStatus.MISMATCH, Severity.HIGH
    elif extra:
        # Nothing the source asked for is absent — an extra index is a review
        # item, not a migration gap.
        status, severity = MatchStatus.MISMATCH, Severity.LOW
    else:
        status, severity = MatchStatus.MATCHED, Severity.INFO

    fqn_src, fqn_tgt = f"{t.schema_name}.{t.table_name}", f"{mapped[0]}.{mapped[1]}"
    total = len(expected)
    return ValidationItem(
        id=f"{kind.value}:{fqn_src}",
        kind=kind,
        source_name=fqn_src,
        target_name=fqn_tgt,
        status=status,
        severity=severity,
        detail=(f"{matched} of {total} {label} present · " + "; ".join(problems)
                if problems else f"All {total} {label} present."),
        recommendation=recommendation if problems else "",
        objects=diffs,
        objects_expected=total,
        objects_present=matched,
        fix_sql="\n\n".join(fixes),
    )


def _table_recommendation(problems: list[str], unverified: bool) -> str:
    """Advice for a table with findings — an unverified count is a scan problem,
    not something re-copying fixes."""
    if not problems:
        return ""
    if unverified:
        return ("Re-run the validation, or check the table exists and is readable in the "
                "target — the row count could not be counted or estimated.")
    return ("Re-copy the table to reconcile the data. Structural gaps can be "
            "fixed with the SQL below or the AI fix.")


_POST_DATA_REC = (
    "These are created after the data load — a gap usually means the post-data "
    "phase did not run or partly failed. Apply the SQL below, or re-run the "
    "migration's post-data step."
)


def post_data_items(
    t: TableInfo,
    inv: TargetInventory,
    mapped: tuple[str, str],
    target_schema: str,
    identifier_case: IdentifierCase | str,
) -> list[ValidationItem]:
    """One rollup item per post-data kind for a single table.

    The expected target names come from ``schema_migration/naming`` — the same
    rules ``ddl_generator`` used to create the objects — and the ``fix_sql`` for
    a missing object is generated by the very function that would have created
    it, so remediation needs no AI and cannot drift from the migration.
    """
    tgt_schema = mapped[0]
    items: list[ValidationItem] = []

    # --- Constraints: PK, checks, column defaults, identity ---
    #
    # These four are one ObjectKind (mirroring the migration plan, where they are
    # all CONSTRAINT items) because they share a cause: they are what the
    # post-data phase adds to a loaded table.
    expected: list[tuple[str, str, str, tuple[str, ...] | None]] = []
    present: list[TargetObject] = []
    fix_for: dict[str, str] = {}
    cols_by_name = {c.name: c for c in t.columns}

    if t.primary_key:
        pk_name = primary_key_name(t.table_name, identifier_case)
        expected.append((pk_name, f"PRIMARY KEY ({_cols(t.primary_key)})", "",
                         tuple(t.primary_key)))
        fix_for[pk_name] = primary_key_ddl(t, tgt_schema, identifier_case)
    pk = inv.primary_keys.get(mapped)
    if pk:
        present.append(pk)

    for chk in t.check_constraints:
        name = mapped_identifier(chk.name, identifier_case)
        expected.append((name, f"CHECK {chk.name}", chk.definition, None))
        fix_for[name] = check_constraint_ddl(
            chk, t.table_name, tgt_schema, identifier_case, t.columns
        )
    present.extend(inv.checks.get(mapped, []))

    # Defaults and identity are column attributes, not named constraints, so they
    # are keyed by column name on both sides.
    target_defaults = inv.column_defaults.get(mapped, set())
    for d in t.column_defaults:
        expected.append((d.column, f"DEFAULT on {d.column}", d.definition, None))
        fix_for[d.column] = column_default_ddl(
            d, t.table_name, tgt_schema, column=cols_by_name.get(d.column),
            target_schema=target_schema, identifier_case=identifier_case,
        )
    if t.identity_column:
        # An identity column also carries a DEFAULT in some target shapes (a
        # sequence-backed default for non-integer types), so accept either
        # signal rather than reporting a correctly-migrated identity as missing.
        ident = t.identity_column
        expected.append((ident, f"IDENTITY on {ident}", "", None))
        fix_for[ident] = identity_ddl(t, tgt_schema, identifier_case)
        if ident in inv.identity_columns.get(mapped, set()):
            target_defaults = target_defaults | {ident}
    # Only column-keyed entries participate here; a target column with a default
    # the source didn't have is reported as extra by the same rollup.
    column_keyed = {d.column for d in t.column_defaults} | (
        {t.identity_column} if t.identity_column else set()
    )
    present.extend(
        TargetObject(name=name) for name in sorted(target_defaults)
        if name in column_keyed or name in inv.columns.get(mapped, {})
    )

    item = _post_data_rollup(
        t, inv, mapped, kind=ObjectKind.CONSTRAINT, expected=expected, present=present,
        fix_for=fix_for, label="constraints", recommendation=_POST_DATA_REC,
    )
    if item:
        items.append(item)

    # --- Indexes (source unique constraints are scanned as unique indexes) ---
    expected, fix_for = [], {}
    for idx in t.indexes:
        name = index_name(idx.name, t.table_name, identifier_case)
        label = f"{'UNIQUE ' if idx.is_unique else ''}INDEX {idx.name}"
        expected.append((name, label, idx.filter_definition or "",
                         tuple(c.name for c in idx.columns)))
        fix_for[name] = index_ddl(idx, t.table_name, tgt_schema, identifier_case, t.columns)
    item = _post_data_rollup(
        t, inv, mapped, kind=ObjectKind.INDEX, expected=expected,
        present=inv.indexes.get(mapped, []), fix_for=fix_for,
        label="indexes", recommendation=_POST_DATA_REC,
    )
    if item:
        items.append(item)

    # --- Foreign keys ---
    expected, fix_for = [], {}
    for fk in t.foreign_keys:
        name = mapped_identifier(fk.name, identifier_case)
        ref = (f"{map_schema(fk.ref_schema, target_schema, identifier_case)}."
               f"{map_object(fk.ref_table, identifier_case)}")
        expected.append((name, f"{fk.name} → {ref} ({_cols(fk.ref_columns)})", "",
                         tuple(fk.columns)))
        fix_for[name] = foreign_key_ddl(
            fk, t.table_name, tgt_schema, target_schema, identifier_case
        )
    item = _post_data_rollup(
        t, inv, mapped, kind=ObjectKind.FOREIGN_KEY, expected=expected,
        present=inv.foreign_keys.get(mapped, []), fix_for=fix_for,
        label="foreign keys", recommendation=_POST_DATA_REC,
    )
    if item:
        items.append(item)

    return items


def expected_schemas(
    tables: list[TableInfo],
    objects: list[ProgrammableObject],
    target_schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> list[str]:
    """The Postgres schemas this migration maps into — the comparison scope."""
    sources = {t.schema_name for t in tables} | {o.schema_name for o in objects}
    mapped = {map_schema(s, target_schema, identifier_case) for s in sources}
    mapped.add(map_schema("dbo", target_schema, identifier_case))  # include default target
    return sorted(mapped)


def compare(
    tables: list[TableInfo],
    objects: list[ProgrammableObject],
    inventory: TargetInventory,
    *,
    target_schema: str = "public",
    source_counts: dict[tuple[str, str], int] | None = None,
    target_counts: dict[tuple[str, str], int] | None = None,
    approximate_counts: set[tuple[str, str]] | None = None,
    source_database: str = "",
    target_database: str = "",
    include_tables: bool = True,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> ValidationReport:
    """Line up the source inventory with the target inventory and diff them.

    ``source_counts``/``target_counts`` hold row counts keyed by the *source*
    (schema, table); tables absent from the maps fall back to the scanner's
    approximate count on the source side and ``None`` on the target.
    ``approximate_counts`` names the keys whose counts are planner estimates
    rather than exact — those are compared with a small tolerance and flagged so
    the UI can disclaim them.

    ``include_tables=False`` (the objects-only re-scan) compares just schemas
    and code objects: callers pass ``tables=[]``, and target tables are not
    flagged as extra — the previous full report already covers them.

    Constraints, indexes, and foreign keys are compared as one rollup item per
    table per kind (see ``post_data_items``), and only when the inventory
    actually read those catalogs.
    """
    source_counts = source_counts or {}
    target_counts = target_counts or {}
    approximate_counts = approximate_counts or set()
    items: list[ValidationItem] = []
    # Post-data kinds are compared whenever the target catalogs were read — the
    # objects scope included, since verifying an agent's constraint fix is
    # exactly what that fast re-scan is for. It stays fast because these are
    # catalog reads on both sides; no table is counted.
    compare_post_data = inventory.post_data_scanned

    # --- Schemas ---
    src_schemas = sorted({t.schema_name for t in tables} | {o.schema_name for o in objects})
    seen_target_schemas: set[str] = set()
    for s in src_schemas:
        mapped = map_schema(s, target_schema, identifier_case)
        if mapped in seen_target_schemas:
            continue
        seen_target_schemas.add(mapped)
        ok = mapped in inventory.schemas
        items.append(ValidationItem(
            id=f"schema:{s}",
            kind=ObjectKind.SCHEMA,
            source_name=s,
            target_name=mapped,
            status=MatchStatus.MATCHED if ok else MatchStatus.MISSING,
            severity=Severity.INFO if ok else Severity.HIGH,
            detail="" if ok else f'Schema "{mapped}" does not exist in the target database.',
            recommendation="" if ok else "Create the schema, then re-apply the objects that live in it.",
            fix_sql="" if ok else schema_ddl(mapped),
        ))

    # --- Tables (existence, structure, rows) ---
    claimed_tables: set[tuple[str, str]] = set()
    for t in tables:
        key = (t.schema_name, t.table_name)
        mapped = (
            map_schema(t.schema_name, target_schema, identifier_case),
            map_object(t.table_name, identifier_case),
        )
        claimed_tables.add(mapped)
        fqn_src, fqn_tgt = f"{t.schema_name}.{t.table_name}", f"{mapped[0]}.{mapped[1]}"

        if mapped not in inventory.tables:
            if include_tables:
                items.append(ValidationItem(
                    id=f"table:{fqn_src}",
                    kind=ObjectKind.TABLE,
                    source_name=fqn_src,
                    target_name=fqn_tgt,
                    status=MatchStatus.MISSING,
                    severity=Severity.HIGH,
                    detail=f'Table "{fqn_tgt}" does not exist in the target database.',
                    recommendation="Apply the generated CREATE TABLE, then copy the data "
                                   "(Data Migration / Create Sync).",
                    source_rows=source_counts.get(key, t.row_count),
                    fix_sql=table_ddl(
                        t, mapped[0], identifier_case,
                        map_schema("dbo", target_schema, identifier_case),
                    ),
                ))
            continue

        target_cols = inventory.columns.get(mapped, {})
        cols_missing = [c.name for c in t.columns if c.name not in target_cols]
        src_col_names = {c.name for c in t.columns}
        cols_extra = sorted(n for n in target_cols if n not in src_col_names)
        drift = [
            f'{c.name}: expected {_expected_pg_type(c)}, found {target_cols[c.name].lower()}'
            for c in t.columns
            if c.name in target_cols and target_cols[c.name].lower() != _expected_pg_type(c)
        ]
        coll_drift = _collation_drift(t, inventory.column_collations.get(mapped, {}), target_cols)

        src_rows = source_counts.get(key, t.row_count)
        tgt_rows = target_counts.get(key)
        approx = key in approximate_counts
        # "Unverified" only when a count pass actually ran for this table (its key
        # is in one of the maps) but yielded no target number — i.e. the count
        # failed. A caller that supplies no counts at all (structure-only compare,
        # objects-only re-scan) is not making a row claim, so don't flag it.
        counted = key in source_counts or key in target_counts
        # Exact counts must match to the row; estimated counts (huge tables) are
        # allowed a small relative slack so normal planner drift isn't a finding.
        rows_differ = tgt_rows is not None and (
            abs(src_rows - tgt_rows) > src_rows * _ESTIMATE_TOLERANCE if approx
            else src_rows != tgt_rows
        )

        problems: list[str] = []
        severity = Severity.INFO
        if tgt_rows is None and counted:
            # A count pass ran but neither counted nor estimated the target — we
            # can't claim the data matches, so don't silently pass it as "matched".
            problems.append("could not verify target row count")
            severity = Severity.MEDIUM
        elif rows_differ:
            qual = "approx. " if approx else ""
            problems.append(f"row counts differ ({qual}source {src_rows:,}, target {tgt_rows:,})")
            severity = Severity.HIGH
        if cols_missing:
            problems.append(f"columns missing in target: {', '.join(cols_missing)}")
            severity = max(severity, Severity.MEDIUM, key=lambda s: _PENALTY[s])
        if drift:
            problems.append(f"column type drift: {'; '.join(drift)}")
            severity = max(severity, Severity.MEDIUM, key=lambda s: _PENALTY[s])
        if coll_drift:
            # MEDIUM like type drift: the data is intact, but string comparison
            # differs from the source until the collation is put back.
            problems.append(f"column collation drift: {'; '.join(coll_drift)}")
            severity = max(severity, Severity.MEDIUM, key=lambda s: _PENALTY[s])
        if cols_extra:
            problems.append(f"extra columns in target: {', '.join(cols_extra)}")
            severity = max(severity, Severity.LOW, key=lambda s: _PENALTY[s])

        # Constraints/indexes/FKs for a table that exists on both sides. A table
        # missing from the target is already a HIGH finding whose fix is the
        # CREATE TABLE plus a re-copy; listing its constraints too would just
        # repeat the same problem three more times.
        if compare_post_data:
            items.extend(
                post_data_items(t, inventory, mapped, target_schema, identifier_case)
            )
        if not include_tables:
            # Objects scope: post-data rollups above are re-checked (they are the
            # agent's work and must reflect the fixes it just applied), but the
            # table's own structure/row-count item is not — no counts were taken,
            # and the previous full report's item carries over in the merge.
            continue

        collation_schema = map_schema("dbo", target_schema, identifier_case)
        readded = [c for c in t.columns if c.name in cols_missing]
        fix_parts: list[str] = []
        # A re-added column COLLATEs its collation, which may never have been created.
        for ddl in dict.fromkeys(
            c.ddl(collation_schema)
            for c in (column_collation(col) for col in readded)
            if c is not None and c.needs_create
        ):
            fix_parts.append(ddl)
        fix_parts.extend(
            _add_column_sql(mapped[0], mapped[1], c, collation_schema) for c in readded
        )
        if coll_drift:
            fix_parts.append(_recollate_sql(
                t, mapped[0], mapped[1], collation_schema,
                inventory.column_collations.get(mapped, {}),
            ))
        fix = "\n".join(p for p in fix_parts if p)
        items.append(ValidationItem(
            id=f"table:{fqn_src}",
            kind=ObjectKind.TABLE,
            source_name=fqn_src,
            target_name=fqn_tgt,
            status=MatchStatus.MISMATCH if problems else MatchStatus.MATCHED,
            severity=severity,
            detail=_sentence("; ".join(problems)) if problems else
                   f"Structure matches · ~{src_rows:,} rows on both sides (estimated)." if approx
                   else f"Structure matches · {src_rows:,} rows on both sides." if tgt_rows is not None
                   else "Structure matches.",
            recommendation=_table_recommendation(problems, unverified=tgt_rows is None and counted),
            source_rows=src_rows,
            target_rows=tgt_rows,
            rows_approximate=approx,
            columns_missing=cols_missing,
            columns_extra=cols_extra,
            type_drift=drift,
            collation_drift=coll_drift,
            fix_sql=fix,
        ))

    # --- Programmable objects ---
    kind_bucket = {
        ObjectKind.VIEW: inventory.views,
        ObjectKind.PROCEDURE: inventory.procedures,
        ObjectKind.FUNCTION: inventory.functions,
        ObjectKind.TRIGGER: inventory.triggers,
    }
    claimed: dict[ObjectKind, set[tuple[str, str]]] = {k: set() for k in kind_bucket}
    for o in objects:
        kind = _OBJECT_TYPE_TO_KIND.get(o.object_type)
        if kind is None:
            continue
        mapped = (
            map_schema(o.schema_name, target_schema, identifier_case),
            map_object(o.object_name, identifier_case),
        )
        claimed[kind].add(mapped)
        # A SQL Server trigger is one object; Postgres splits it into a trigger
        # plus a companion trigger function the migration creates as
        # ``<trigger>_fn`` (schema_migration/ai_translator). Claim that function
        # so it isn't reported as an "extra" with no source, and — when it really
        # exists in the target — surface it as a *matched* function that explains
        # the two-object model, so it doesn't just silently vanish from the
        # Functions list a SQL Server user is scanning.
        if kind is ObjectKind.TRIGGER:
            fn_key = (mapped[0], trigger_function_name(o.object_name, identifier_case))
            claimed[ObjectKind.FUNCTION].add(fn_key)
            if fn_key in inventory.functions:
                items.append(ValidationItem(
                    id=f"trigger-fn:{o.schema_name}.{o.object_name}",
                    kind=ObjectKind.FUNCTION,
                    source_name="",  # no standalone source object — it backs the trigger
                    target_name=f"{fn_key[0]}.{fn_key[1]}",
                    status=MatchStatus.MATCHED,
                    severity=Severity.INFO,
                    detail=f'Trigger function backing trigger "{o.schema_name}.{o.object_name}". '
                           "PostgreSQL implements a trigger as two objects — the trigger and a "
                           "separate function holding its logic — so the migration created this "
                           "function alongside the trigger. There is no standalone equivalent in "
                           "SQL Server, where the logic lives inside the trigger itself.",
                ))
        fqn_src, fqn_tgt = f"{o.schema_name}.{o.object_name}", f"{mapped[0]}.{mapped[1]}"
        ok = mapped in kind_bucket[kind]
        items.append(ValidationItem(
            id=f"{kind.value}:{fqn_src}",
            kind=kind,
            source_name=fqn_src,
            target_name=fqn_tgt,
            status=MatchStatus.MATCHED if ok else MatchStatus.MISSING,
            severity=Severity.INFO if ok else Severity.HIGH,
            detail="" if ok else f'{kind.value.capitalize()} "{fqn_tgt}" does not exist in the '
                                 "target database.",
            recommendation="" if ok else "Translate and create it — use the AI fix to convert "
                                         "the T-SQL definition to Postgres, review, and apply.",
            source_definition="" if ok else o.definition,
        ))

    # --- Extra objects in the target (within the migration's schemas) ---
    def extras(kind: ObjectKind, present: set[tuple[str, str]], owned: set[tuple[str, str]]):
        for schema, name in sorted(present - owned):
            hint = (" It may also be a helper the migration created (e.g. a trigger function)."
                    if kind is ObjectKind.FUNCTION else "")
            items.append(ValidationItem(
                id=f"extra-{kind.value}:{schema}.{name}",
                kind=kind,
                target_name=f"{schema}.{name}",
                status=MatchStatus.EXTRA,
                severity=Severity.LOW,
                detail=f'{kind.value.capitalize()} "{schema}.{name}" exists in Lakebase but not '
                       "in the source.",
                recommendation="Verify it is intentional; use Remove from target to drop "
                               "it if it should not exist." + hint,
                fix_sql=_drop_sql(kind, schema, name),
            ))

    if include_tables:
        extras(ObjectKind.TABLE, inventory.tables, claimed_tables)
    extras(ObjectKind.VIEW, inventory.views, claimed[ObjectKind.VIEW])
    extras(ObjectKind.PROCEDURE, inventory.procedures, claimed[ObjectKind.PROCEDURE])
    extras(ObjectKind.FUNCTION, inventory.functions, claimed[ObjectKind.FUNCTION])
    extras(ObjectKind.TRIGGER, inventory.triggers, claimed[ObjectKind.TRIGGER])

    return _rollup(items, target_schema=target_schema,
                   source_database=source_database, target_database=target_database)


def _rollup(
    items: list[ValidationItem], *, target_schema: str, source_database: str, target_database: str
) -> ValidationReport:
    """Sort the items and derive the report stats (counts, score, row totals)."""
    items.sort(key=lambda i: (KIND_ORDER.get(i.kind, 9), i.source_name or i.target_name))
    counts = {s: sum(i.status is s for i in items) for s in MatchStatus}
    # Plain percentage of compared objects that matched; int() floors, so 100
    # is only reachable when every object matches.
    score = int(100 * counts[MatchStatus.MATCHED] / len(items)) if items else 100
    # Row totals must cover the same tables on both sides to be comparable: a
    # table missing in the target (or one whose count could be neither counted
    # nor estimated) has no target number, so it would inflate the source total
    # while contributing nothing to the target total. Estimated counts (huge
    # tables) DO have both numbers and are included — the totals just carry the
    # same approximation, disclaimed in the UI.
    counted = [
        i for i in items
        if i.kind is ObjectKind.TABLE and i.source_name and i.target_rows is not None
    ]
    return ValidationReport(
        source_database=source_database,
        target_database=target_database,
        target_schema=target_schema,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        match_score=score,
        total_source=sum(1 for i in items if i.source_name),
        matched=counts[MatchStatus.MATCHED],
        missing=counts[MatchStatus.MISSING],
        mismatched=counts[MatchStatus.MISMATCH],
        extra=counts[MatchStatus.EXTRA],
        source_rows=sum(i.source_rows or 0 for i in counted),
        target_rows=sum(i.target_rows or 0 for i in counted),
        tables_compared=len(counted),
        tables_estimated=sum(1 for i in counted if i.rows_approximate),
        items=items,
    )


def merge_object_rescan(previous: ValidationReport, rescan: ValidationReport) -> ValidationReport:
    """Overlay an objects-only re-scan onto the previous full report.

    Schema, code-object, and post-data (constraint/index/FK) items come from the
    re-scan, so a fixed procedure or a newly created foreign key flips to
    matched, and one the agent failed to fix shows missing again. Only the table
    items — structure and row counts — carry over unchanged, keeping the hero
    stats meaningful without re-counting every table. A schema that hosts only
    tables is invisible to the objects scope (its schemas derive from the code
    objects), so its previous item carries over too instead of vanishing.

    Post-data items must NOT be carried over: they are precisely what the repair
    agent fixes, so a stale copy would keep reporting a constraint the agent just
    created as missing.
    """
    fresh = [i for i in rescan.items if i.kind is not ObjectKind.TABLE]
    fresh_ids = {i.id for i in fresh}
    kept = [
        i.model_copy(deep=True) for i in previous.items
        if i.kind is ObjectKind.TABLE
        or (i.kind is ObjectKind.SCHEMA and i.id not in fresh_ids)
    ]
    return _rollup(kept + fresh, target_schema=previous.target_schema,
                   source_database=rescan.source_database or previous.source_database,
                   target_database=rescan.target_database or previous.target_database)


# --- Orchestration ------------------------------------------------------------------


def run_validation(
    source,
    target: LakebaseConnection,
    target_schema: str = "public",
    progress: ProgressFn | None = None,
    scope: str = "full",
    use_estimates: bool = True,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> ValidationReport:
    """Full source-vs-target validation. ``source`` is any connector exposing
    ``query()``/``database`` (see connectors/factory.py).

    ``scope="objects"`` re-checks only schemas and code objects — no table
    structure and, above all, no per-table ``COUNT(*)`` — so it finishes in
    seconds. Used to verify the AI repair agent's fixes right after they apply.

    ``use_estimates`` (default) counts tables above ``_EXACT_COUNT_MAX_ROWS`` by
    planner estimate to avoid a slow full scan; set ``False`` to force an exact
    ``COUNT(*)`` on every table (with a generous timeout) for precise totals.
    """
    notify = progress or (lambda *_: None)
    objects_only = scope == "objects"

    notify("Scanning source", 0, 0, "")
    # Tables are scanned in both scopes: the objects scope skips their *row
    # counts* (the slow part), but it still needs the table metadata to re-check
    # constraints, indexes, and foreign keys — which the repair agent fixes, and
    # which this fast pass exists to verify. The scan is catalog-only.
    tables = scan_tables(source)
    objects = scan_objects(source)

    notify("Scanning Lakebase", 0, 0, "")
    schemas = expected_schemas(tables, objects, target_schema, identifier_case)
    inventory = fetch_target_inventory(target, schemas)

    # Count only where the table exists on both sides — a missing table is
    # already a finding; counting it would just fail. The objects scope counts
    # nothing at all: that is what makes it finish in seconds.
    both = [] if objects_only else [
        t for t in tables
        if (
            map_schema(t.schema_name, target_schema, identifier_case),
            map_object(t.table_name, identifier_case),
        ) in inventory.tables
    ]
    source_counts: dict[tuple[str, str], int] = {}
    target_counts: dict[tuple[str, str], int] = {}
    approximate: set[tuple[str, str]] = set()
    for done, t in enumerate(both):
        fqn = f"{t.schema_name}.{t.table_name}"
        notify("Comparing row counts", done, len(both), fqn)
        key = (t.schema_name, t.table_name)
        tgt_schema = map_schema(t.schema_name, target_schema, identifier_case)
        tgt_table = map_object(t.table_name, identifier_case)
        # In estimate mode, above the threshold an exact COUNT(*) risks a
        # server-side timeout, so count both sides by estimate instead of
        # stranding the target as blank. Exact mode counts every table for real.
        estimate = use_estimates and t.row_count > _EXACT_COUNT_MAX_ROWS
        # A big exact count gets a generous timeout so the full scan can finish.
        timeout = None if estimate or t.row_count <= _EXACT_COUNT_MAX_ROWS else _EXACT_COUNT_TIMEOUT_SECONDS
        # Source and target are counted independently: one side failing must not
        # leave the other unset (that mislabelled the table "matched").
        try:
            source_counts[key] = (
                t.row_count if estimate
                else count_source_rows(source, t.schema_name, t.table_name, timeout=timeout)
            )
        except Exception as exc:
            log.warning("Source row count failed for %s: %s", fqn, exc)
        try:
            target_counts[key] = (
                estimate_target_rows(target, tgt_schema, tgt_table) if estimate
                else count_target_rows(target, tgt_schema, tgt_table, timeout=timeout)
            )
        except Exception as exc:
            log.warning("Target row count failed for %s: %s", fqn, exc)
            # In estimate mode an exact count that timed out still gets an
            # estimate rather than a blank target — a blank silently passed as
            # "matched". In exact mode the user asked for real counts, so we
            # don't quietly substitute an estimate: leave it unverified.
            if not estimate and use_estimates:
                log.info("Falling back to a row-count estimate for %s", fqn)
                try:
                    target_counts[key] = estimate_target_rows(target, tgt_schema, tgt_table)
                    estimate = True
                except Exception as exc2:
                    log.warning("Target row estimate also failed for %s: %s", fqn, exc2)
        if estimate:
            approximate.add(key)
    notify("Building report", len(both), len(both), "")

    return compare(
        tables, objects, inventory,
        target_schema=target_schema,
        source_counts=source_counts,
        target_counts=target_counts,
        approximate_counts=approximate,
        source_database=getattr(source, "database", ""),
        target_database=target.database,
        include_tables=not objects_only,
        identifier_case=identifier_case,
    )
