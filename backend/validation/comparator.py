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
from backend.schema_migration.ddl_generator import schema_ddl, table_ddl
from backend.schema_migration.naming import (
    IdentifierCase,
    map_object,
    map_schema,
    trigger_function_name,
)
from backend.schema_migration.type_mapper import map_type
from backend.validation.models import MatchStatus, ValidationItem, ValidationReport

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

_PG_COLUMNS_SQL = """
SELECT table_schema AS schema, table_name AS "table", column_name AS name, data_type
FROM   information_schema.columns
WHERE  table_schema = ANY(%(schemas)s)
"""


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


def fetch_target_inventory(conn: LakebaseConnection, schemas: list[str]) -> TargetInventory:
    params = {"schemas": schemas}
    inv = TargetInventory(
        schemas={r["name"] for r in conn.query(_PG_SCHEMAS_SQL)},
        tables={(r["schema"], r["name"]) for r in conn.query(_PG_TABLES_SQL, params)},
        views={(r["schema"], r["name"]) for r in conn.query(_PG_VIEWS_SQL, params)},
        triggers={(r["schema"], r["name"]) for r in conn.query(_PG_TRIGGERS_SQL, params)},
    )
    for r in conn.query(_PG_ROUTINES_SQL, params):
        bucket = inv.procedures if r["kind"] == "procedure" else inv.functions
        bucket.add((r["schema"], r["name"]))
    for r in conn.query(_PG_COLUMNS_SQL, params):
        inv.columns.setdefault((r["schema"], r["table"]), {})[r["name"]] = r["data_type"]
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


def _add_column_sql(schema: str, table: str, col) -> str:
    null = "" if col.is_nullable else " NOT NULL"
    return (
        f"ALTER TABLE {_pg_ident(schema)}.{_pg_ident(table)} "
        f'ADD COLUMN "{col.name}" {map_type(col)}{null};'
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
    """
    source_counts = source_counts or {}
    target_counts = target_counts or {}
    approximate_counts = approximate_counts or set()
    items: list[ValidationItem] = []

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
                fix_sql=table_ddl(t, mapped[0], identifier_case),
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
        if cols_extra:
            problems.append(f"extra columns in target: {', '.join(cols_extra)}")
            severity = max(severity, Severity.LOW, key=lambda s: _PENALTY[s])

        fix = "\n".join(_add_column_sql(mapped[0], mapped[1], c)
                        for c in t.columns if c.name in cols_missing)
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

    Schema and code-object items come from the re-scan (so a fixed procedure
    flips to matched, and one the agent failed to fix shows missing again);
    table items — structure and row counts — carry over unchanged, keeping the
    hero stats meaningful without re-counting every table. A schema that hosts
    only tables is invisible to the objects scope (its schemas derive from the
    code objects), so its previous item carries over too instead of vanishing.
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
    tables = [] if objects_only else scan_tables(source)
    objects = scan_objects(source)

    notify("Scanning Lakebase", 0, 0, "")
    schemas = expected_schemas(tables, objects, target_schema, identifier_case)
    inventory = fetch_target_inventory(target, schemas)

    # Count only where the table exists on both sides — a missing table is
    # already a finding; counting it would just fail.
    both = [
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
