"""Builds an editable migration plan from the assessment results.

Each plan item carries the exact SQL that will be applied to Lakebase, so the
user can review and edit it before execution. Table DDL is generated from the
type mapper; code objects are (optionally) translated by the Foundation Model.

The plan has two phases. Pre-data items (schemas, collations, tables, functions,
views, procedures) are applied before the data load; post-data items (constraints,
indexes, foreign keys, triggers — see POST_DATA_KINDS) only after it, so the
bulk COPY runs against bare tables and identity sequences can sync to the
loaded data.
"""
from __future__ import annotations

from typing import Callable

from backend.assessment.models import ProgrammableObject, TableInfo
from backend.migration.models import ObjectKind, PlanItem
from backend.schema_migration.ai_translator import translate_all
from backend.schema_migration.collation_mapper import collect_collations
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
from backend.schema_migration.naming import IdentifierCase, map_object, map_schema

_KIND_FROM_TYPE = {
    "PROCEDURE": ObjectKind.PROCEDURE,
    "VIEW": ObjectKind.VIEW,
    "FUNCTION": ObjectKind.FUNCTION,
    "TRIGGER": ObjectKind.TRIGGER,
}


def build_plan(
    tables: list[TableInfo],
    objects: list[ProgrammableObject],
    target_schema: str,
    translate: bool,
    endpoint: str | None,
    on_translate_progress: Callable[[int, int], None] | None = None,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> list[PlanItem]:
    """Build the editable migration plan. ``on_translate_progress(done, total)``
    is called as code-object translations complete (for background-run progress);
    it runs from translation worker threads, so it must be thread-safe."""
    # Source schemas are preserved 1:1 (dbo -> target_schema/public, others
    # mapped by the project's case policy), so create one Postgres schema per
    # distinct source schema.
    sources = [t.schema_name for t in tables] + [o.schema_name for o in objects]
    schema_map = {s: map_schema(s, target_schema, identifier_case) for s in sources}

    # Created before the tables that reference them, all in the default target
    # schema since a collation is shared across schemas.
    collation_schema = map_schema("dbo", target_schema, identifier_case)
    usage = collect_collations(tables)

    # The collation schema gets an item too, even when no table maps to it — the
    # collations are created there and would otherwise have nowhere to land.
    schemas = set(schema_map.values()) | ({collation_schema} if usage.created() else set())
    items: list[PlanItem] = [
        PlanItem(
            id=f"schema:{ms}",
            kind=ObjectKind.SCHEMA,
            name=ms,
            sql=schema_ddl(ms),
            notes="Creates the target schema if it does not exist.",
        )
        for ms in sorted(schemas)
    ]

    for coll in usage.created():
        used_by = usage.columns.get(coll.name, [])
        source_name = coll.source.name if coll.source else coll.name
        strength = coll.source.strength_label if coll.source else ""
        note = (
            f"Mirrors the source collation {source_name} ({strength}) so string "
            f"comparison keeps the source's semantics. Used by {len(used_by)} column(s)."
        )
        if not coll.deterministic:
            note += (
                " Created as nondeterministic — that is what makes equality itself "
                "ignore case/accents, as in SQL Server. Postgres does not support "
                "LIKE or pattern matching on these columns."
            )
        if coll.locale_fallback:
            note += (
                f" The source locale was not recognised, so the ICU root locale is used "
                f"with the same strength — review the sort order if this collation is "
                f"language-specific."
            )
        items.append(
            PlanItem(
                id=f"collation:{source_name}",
                kind=ObjectKind.COLLATION,
                name=f"{collation_schema}.{coll.name}",
                sql=coll.ddl(collation_schema),
                notes=note,
            )
        )

    for t in tables:
        ms = schema_map[t.schema_name]
        items.append(
            PlanItem(
                id=f"table:{t.schema_name}.{t.table_name}",
                kind=ObjectKind.TABLE,
                name=f"{ms}.{map_object(t.table_name, identifier_case)}",
                sql=table_ddl(t, ms, identifier_case, collation_schema),
                notes=f"{t.column_count} columns · {t.row_count:,} rows · source {t.fqn}.",
            )
        )

    items.extend(_post_data_items(tables, schema_map, target_schema, identifier_case))

    # Code objects — translate up front (one batch) if requested. The schema map
    # is passed so generated code qualifies references with the mapped schemas.
    translations = {}
    if translate and objects:
        translate_kwargs = {
            "schema_map": schema_map,
            "on_done": on_translate_progress,
        }
        # Keep the default call shape backward-compatible for extensions/tests
        # that wrap translate_all; only the new opt-in needs the extra argument.
        if identifier_case == IdentifierCase.PRESERVE:
            translate_kwargs["identifier_case"] = identifier_case
        for tr in translate_all(objects, endpoint, **translate_kwargs):
            translations[tr.object_name] = tr

    for o in objects:
        kind = _KIND_FROM_TYPE.get(o.object_type.upper(), ObjectKind.PROCEDURE)
        full = f"{o.schema_name}.{o.object_name}"
        ms = schema_map[o.schema_name]
        tr = translations.get(full)
        items.append(
            PlanItem(
                id=f"{kind.value}:{full}",
                kind=kind,
                name=f"{ms}.{map_object(o.object_name, identifier_case)}",
                sql=(tr.translated if tr else ""),
                original=o.definition,
                reasoning=(tr.reasoning if tr else ""),
                notes=(tr.notes if tr else "Enable AI translation, or paste Postgres SQL to apply."),
            )
        )

    return items


_POST_DATA_NOTE = "Applied after the data load (bulk-copy performance)."


def _post_data_items(
    tables: list[TableInfo],
    schema_map: dict[str, str],
    target_schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> list[PlanItem]:
    """Constraint/index/FK items for the post-data phase. Emitted per object so
    a single failure stays isolated and each statement is reviewable/editable;
    KIND_ORDER guarantees constraints → indexes → FKs across tables."""
    items: list[PlanItem] = []
    for t in tables:
        ms = schema_map[t.schema_name]
        target = f"{ms}.{map_object(t.table_name, identifier_case)}"
        src = f"{t.schema_name}.{t.table_name}"
        cols = {c.name: c for c in t.columns}

        for d in t.column_defaults:
            items.append(
                PlanItem(
                    id=f"default:{src}.{d.column}",
                    kind=ObjectKind.CONSTRAINT,
                    name=f"{target} · DEFAULT {d.column}",
                    sql=column_default_ddl(d, t.table_name, ms,
                                           column=cols.get(d.column), target_schema=target_schema,
                                           identifier_case=identifier_case),
                    notes=f"Column default (source: {d.definition}) — translated mechanically, "
                          f"review if it used a T-SQL-only function. {_POST_DATA_NOTE}",
                )
            )
        if t.identity_column:
            items.append(
                PlanItem(
                    id=f"identity:{src}.{t.identity_column}",
                    kind=ObjectKind.CONSTRAINT,
                    name=f"{target} · IDENTITY {t.identity_column}",
                    sql=identity_ddl(t, ms, identifier_case),
                    notes="Recreates the IDENTITY as a Postgres identity/sequence and syncs it "
                          f"to MAX({t.identity_column})+1 — which is why it runs after the load.",
                )
            )
        if t.primary_key:
            items.append(
                PlanItem(
                    id=f"pk:{src}",
                    kind=ObjectKind.CONSTRAINT,
                    name=f"{target} · PRIMARY KEY",
                    sql=primary_key_ddl(t, ms, identifier_case),
                    notes=f"PRIMARY KEY ({', '.join(t.primary_key)}). {_POST_DATA_NOTE}",
                )
            )
        for chk in t.check_constraints:
            items.append(
                PlanItem(
                    id=f"check:{src}.{chk.name}",
                    kind=ObjectKind.CONSTRAINT,
                    name=f"{target} · CHECK {map_object(chk.name, identifier_case)}",
                    sql=check_constraint_ddl(chk, t.table_name, ms, identifier_case, t.columns),
                    notes=f"Check constraint (source: {chk.definition}) — translated "
                          f"mechanically, review the predicate. {_POST_DATA_NOTE}",
                )
            )

    for t in tables:
        ms = schema_map[t.schema_name]
        target = f"{ms}.{map_object(t.table_name, identifier_case)}"
        for idx in t.indexes:
            cols = ", ".join(c.name for c in idx.columns)
            items.append(
                PlanItem(
                    id=f"index:{t.schema_name}.{t.table_name}.{idx.name}",
                    kind=ObjectKind.INDEX,
                    name=f"{target} · {map_object(idx.name, identifier_case)}",
                    sql=index_ddl(idx, t.table_name, ms, identifier_case, t.columns),
                    notes=f"{'Unique index' if idx.is_unique else 'Index'} on ({cols}). "
                          f"{_POST_DATA_NOTE}",
                )
            )

    for t in tables:
        ms = schema_map[t.schema_name]
        target = f"{ms}.{map_object(t.table_name, identifier_case)}"
        for fk in t.foreign_keys:
            items.append(
                PlanItem(
                    id=f"fk:{t.schema_name}.{t.table_name}.{fk.name}",
                    kind=ObjectKind.FOREIGN_KEY,
                    name=f"{target} · {map_object(fk.name, identifier_case)}",
                    sql=foreign_key_ddl(
                        fk, t.table_name, ms, target_schema, identifier_case
                    ),
                    notes=f"References {map_schema(fk.ref_schema, target_schema, identifier_case)}."
                          f"{map_object(fk.ref_table, identifier_case)} ({', '.join(fk.ref_columns)}). "
                          "Applied last, after all data and primary keys.",
                )
            )
    return items
