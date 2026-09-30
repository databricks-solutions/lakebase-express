"""Builds a ContextBundle from a persisted project.

Pure by design: it reads the project row and opens nothing — no source
connection, no Lakebase connection, no Foundation Model call. That is what lets
the bundle be exported at any phase, cost nothing to produce, and stay
reproducible from stored state alone.

Everything is a projection of what is already persisted. Three joins do the real
work:

  * source expressions (assessment) x translated SQL (plan), keyed by the plan
    item id, which is where DEFAULT/CHECK before-and-after comes from — plan
    items carry ``original`` only for code objects;
  * rule findings x the rewrite table, so the table says which constructs this
    database actually proved;
  * scanned columns x the type/collation mappers, filtered to the changes
    application code can observe.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from backend.assessment import callable_shape
from backend.assessment.models import AssessmentReport, Severity, TableInfo
from backend.assessment.temp_objects import KIND_RULE_IDS
from backend.context_bundle import rules
from backend.context_bundle.models import (
    BUNDLE_VERSION,
    AiNotes,
    CallableContract,
    ColumnContract,
    ContextBundle,
    ExpressionChange,
    KnownGap,
    NameChange,
    OperationalContract,
    Provenance,
    SectionIndex,
    SourceSummary,
    TargetContract,
    sql_digest,
)
from backend.migration.models import ObjectKind, PlanItem
from backend.projects.models import Project
from backend.query_parity.models import QueryParityReport
from backend.schema_migration.collation_mapper import column_collation
from backend.schema_migration.naming import (
    IdentifierCase,
    index_name,
    map_object,
    map_schema,
    primary_key_name,
    trigger_function_name,
)
from backend.schema_migration.type_mapper import map_type
from backend.validation.models import MatchStatus, ValidationReport

# Source types whose Postgres form is `text` legitimately, so a `text` target is
# not the type mapper's unknown-type fallback.
_TEXTUAL = frozenset({"text", "ntext", "varchar", "nvarchar", "char", "nchar", "sysname"})

_CODE_KINDS = frozenset(
    {ObjectKind.PROCEDURE, ObjectKind.VIEW, ObjectKind.FUNCTION, ObjectKind.TRIGGER}
)


# Keyed on what the target IS, not on what the source was: one call form is stated,
# never two offered (see assessment/callable_shape).
def _call_change(
    source_type: str, target: str, kind: str, returns_set: bool, source_returns_rows: bool,
    kind_conflict: bool = False,
) -> str:
    if not kind:
        return ""
    if kind_conflict:
        return (
            f"**Do not rely on a call form for this one yet.** The migration created a "
            f"{kind} named {target}, but validation still finds a "
            f"{source_type.lower()} of that name in the target — both exist, and "
            "PostgreSQL picks between them by argument type, so a caller can reach "
            "either. Drop the stale one first (see the known gaps); until then any "
            "call may fail with SQLSTATE 42809."
        )
    if kind == callable_shape.PROCEDURE:
        rule = (
            f"`EXEC dbo.X @a = 1` becomes `CALL {target}(...)`, and named arguments "
            "become positional or `=>` notation. OUT parameters become INOUT — read "
            "them from the CALL result."
        )
        if source_returns_rows:
            rule += (
                " **This one cannot serve its caller as it stands**: the source returned "
                "a result set and a Postgres procedure cannot. See the known gaps — the "
                "fix is a database change, not an application one."
            )
        return rule
    if kind == callable_shape.FUNCTION:
        how = f"`SELECT * FROM {target}(...)`" if returns_set else f"`SELECT {target}(...)`"
        if source_type.upper() == "PROCEDURE":
            return (
                f"`EXEC dbo.X @a = 1` becomes {how} — it is a **function** in the target, "
                "not a procedure, because it returns a result set. `CALL` fails with "
                "SQLSTATE 42809."
            )
        if returns_set:
            return (
                f"Table-valued: queried as {how} rather than joined directly; argument "
                "order and types are unchanged."
            )
        return f"`SELECT dbo.X(a)` becomes {how}; argument order and types are unchanged."
    if kind == callable_shape.VIEW:
        return f"Referenced as `{target}`; the query shape is unchanged."
    return "Fires on its own as before — application code does not call it."


# --- Parsing the stored row -------------------------------------------------------


def _assessment(project: Project) -> AssessmentReport | None:
    if not project.assessment:
        return None
    try:
        return AssessmentReport.model_validate(project.assessment)
    except Exception:
        return None  # a report from an older shape must not break the export


def _plan(project: Project) -> list[PlanItem]:
    items: list[PlanItem] = []
    for raw in project.plan or []:
        try:
            items.append(PlanItem.model_validate(raw))
        except Exception:
            continue
    return items


def _validation(project: Project) -> ValidationReport | None:
    if not project.validation:
        return None
    try:
        return ValidationReport.model_validate(project.validation)
    except Exception:
        return None


def _parity(project: Project) -> QueryParityReport | None:
    if not project.query_parity:
        return None
    try:
        return QueryParityReport.model_validate(project.query_parity)
    except Exception:
        return None


# --- Sections ---------------------------------------------------------------------


def _target(project: Project) -> TargetContract:
    ic = project.identifier_case
    preserve = ic == IdentifierCase.PRESERVE or ic == "preserve"
    return TargetContract(
        target_schema=project.target_schema,
        identifier_case=ic.value if isinstance(ic, IdentifierCase) else str(ic),
        quoting_rule=rules.QUOTING_PRESERVE if preserve else rules.QUOTING_LOWERCASE,
        notes=list(rules.TARGET_NOTES),
    )


def _target_tables(report: AssessmentReport, plan: list[PlanItem]) -> list[TableInfo]:
    """Tables that exist in the target: the ones the plan created, or every scanned
    table when no plan was built yet."""
    created = {i.id.split(":", 1)[1] for i in plan if i.kind is ObjectKind.TABLE}
    if not created:
        return list(report.tables)
    return [t for t in report.tables if t.fqn in created]


def _names(
    report: AssessmentReport, tables: list[TableInfo], project: Project
) -> list[NameChange]:
    """Renames application code cannot derive from the casing rule alone.

    Schemas and tables are listed because they appear at every call site; primary
    keys because their source name is discarded outright; unique indexes because a
    unique violation or ON CONFLICT names one. Non-unique index, check and foreign
    key names follow the stated `<table>_<name>` + casing rules mechanically, so
    listing each one would restate a rule rather than add information.
    """
    ic = project.identifier_case
    schema = project.target_schema
    out: list[NameChange] = []

    sources = {t.schema_name for t in tables} | {
        o.schema_name for o in report.programmable_objects
    }
    for s in sorted(sources):
        target = map_schema(s, schema, ic)
        if target != s:
            note = "The source default schema `dbo` maps to the project's target schema." if s.lower() == "dbo" else ""
            out.append(NameChange(kind="schema", source=s, target=target, note=note))

    for t in tables:
        ms = map_schema(t.schema_name, schema, ic)
        target = f"{ms}.{map_object(t.table_name, ic)}"
        if target != t.fqn:
            out.append(NameChange(kind="table", source=t.fqn, target=target))
        if t.primary_key:
            out.append(
                # No per-entry note: the derivation rule is stated once in
                # operational.notes rather than repeated on every table.
                NameChange(
                    kind="constraint",
                    source=f"{t.fqn} primary key",
                    target=primary_key_name(t.table_name, ic),
                )
            )
        for idx in t.indexes:
            if not idx.is_unique:
                continue
            out.append(
                NameChange(
                    kind="index",
                    source=f"{t.fqn}.{idx.name}",
                    target=index_name(idx.name, t.table_name, ic),
                )
            )

    for o in report.programmable_objects:
        if o.object_type.upper() != "TRIGGER":
            continue
        ms = map_schema(o.schema_name, schema, ic)
        out.append(
            NameChange(
                kind="trigger_function",
                source=f"{o.schema_name}.{o.object_name}",
                target=f"{ms}.{trigger_function_name(o.object_name, ic)}",
                note="New object: Postgres needs a companion function for the trigger.",
            )
        )
    return out


def _columns(tables: list[TableInfo], project: Project) -> list[ColumnContract]:
    """Columns whose contract with application code changed.

    A column is emitted only when its value semantics, or the way it compares,
    changed — not merely because the type is spelled differently. That filter is
    what keeps the section bounded: most columns round-trip with nothing for an
    application to do.
    """
    ic = project.identifier_case
    schema = project.target_schema
    out: list[ColumnContract] = []

    for t in tables:
        ms = map_schema(t.schema_name, schema, ic)
        target_table = f"{ms}.{map_object(t.table_name, ic)}"
        for col in t.columns:
            source_type = col.data_type.lower()
            target_type = map_type(col)
            # Keys into the glossary; the source type name doubles as its key.
            changes: list[str] = []

            if source_type in rules.TYPE_APP_IMPACT:
                changes.append(source_type)
            elif target_type == "text" and source_type not in _TEXTUAL:
                changes.append(rules.TEXT_FALLBACK)

            collation = column_collation(col)
            if collation is not None and not collation.deterministic:
                changes.append(rules.NONDETERMINISTIC_COLLATION)
            if collation is not None and collation.locale_fallback:
                changes.append(rules.COLLATION_LOCALE_FALLBACK)

            if not changes:
                continue
            out.append(
                ColumnContract(
                    table=target_table,
                    source_table=t.fqn,
                    column=col.name,
                    source_type=col.data_type,
                    target_type=target_type,
                    changes=changes,
                    collation=(collation.name if collation else None),
                    deterministic=(collation.deterministic if collation else True),
                    rejects_pattern_match=bool(collation and not collation.deterministic),
                )
            )
    return out


def _callables(
    plan: list[PlanItem], project: Project, validation: ValidationReport | None = None
) -> list[CallableContract]:
    """Call sites, reconciled against the target when a validation run exists.

    The plan records the SQL the migration *intended*. Reading the created kind from
    it is right up until the apply did not take — a reshaped routine cannot replace
    the other kind — after which the plan says function and the database says
    procedure. Validation is the only record of what is actually there.
    """
    # What validation actually found in the target, per plan item id. Validation
    # reports the kind it matched, so this is a fact rather than an inference —
    # a reshaped procedure legitimately matches as a function.
    found_kind = {
        item.id: item.target_kind
        for item in (validation.items if validation else [])
        if item.target_kind
    }
    ic = project.identifier_case
    out: list[CallableContract] = []
    for item in plan:
        if item.kind not in _CODE_KINDS:
            continue
        object_type = item.kind.value.upper()
        source = item.id.split(":", 1)[1] if ":" in item.id else item.id
        translated = bool(item.sql.strip())
        # Read from the SQL that was produced, not inferred from the source type: a
        # procedure returning a result set has to become a function, and the call
        # form the application must use follows what actually exists.
        kind = callable_shape.target_kind(item.sql)
        returns_set = callable_shape.returns_set(item.sql)
        source_returns_rows = (
            object_type == "PROCEDURE" and callable_shape.returns_result_set(item.original)
        )
        # The plan created one kind and validation found the other: both exist, or the
        # apply never took. Either way no single call form can be promised.
        in_target = found_kind.get(item.id, "")
        kind_conflict = bool(kind and in_target and kind != in_target)
        if not translated:
            provenance = "not-translated"
        else:
            provenance = "ai" if item.reasoning.strip() else "user-edited"
        companion = ""
        if object_type == "TRIGGER":
            name = source.split(".")[-1]
            companion = trigger_function_name(name, ic)
        out.append(
            CallableContract(
                object_type=object_type,
                target_kind=kind,
                returns_set=returns_set,
                source_returns_result_set=source_returns_rows,
                kind_conflict=kind_conflict,
                source=source,
                target=item.name,
                call_change=_call_change(
                    object_type, item.name, kind, returns_set, source_returns_rows,
                    kind_conflict,
                ),
                translated=translated,
                provenance=provenance,
                companion_function=companion,
                note=(
                    item.notes
                    if translated
                    else "Not translated — the application must provide this logic itself."
                ),
            )
        )
    return out


def _expressions(
    tables: list[TableInfo], plan: list[PlanItem]
) -> tuple[list[ExpressionChange], int]:
    """DEFAULT, CHECK and filtered-index predicates, before and after, plus how
    many were left out.

    The join the section needs: plan items carry ``original`` only for code
    objects, so the source text comes from the assessment and is matched to its
    translated statement by the plan item id.

    Only predicates that carry an action are emitted — one with residual T-SQL
    (``risk``) or one the translator passed through verbatim (``passed_through``,
    worth confirming the semantics of). A predicate that translated cleanly tells
    application code nothing: it does not write DEFAULTs or CHECKs, and the
    constructs themselves are in ``rewrite_rules``.
    """
    by_id = {i.id: i for i in plan}
    out: list[ExpressionChange] = []
    omitted = 0

    def add(item_id: str, kind: str, source_expr: str) -> None:
        nonlocal omitted
        item = by_id.get(item_id)
        if item is None or not source_expr:
            return
        target_sql = item.sql
        change = ExpressionChange(
            item_id=item_id,
            kind=kind,
            target_object=item.name,
            source_expr=source_expr,
            target_expr=target_sql,
            passed_through=_passed_through(source_expr, target_sql),
            risk=rules.residue_risk(target_sql),
        )
        if change.passed_through or change.risk:
            out.append(change)
        else:
            omitted += 1

    for t in tables:
        for d in t.column_defaults:
            add(f"default:{t.fqn}.{d.column}", "default", d.definition)
        for chk in t.check_constraints:
            add(f"check:{t.fqn}.{chk.name}", "check", chk.definition)
        for idx in t.indexes:
            if idx.filter_definition:
                add(f"index:{t.fqn}.{idx.name}", "index_filter", idx.filter_definition)
    return out, omitted


def _core(expr: str) -> str:
    """Comparable core of an expression: no whitespace, brackets, quotes or parens.

    Lets a pass-through be recognised through the cosmetic changes the translator
    always makes — `[x]` becomes `"x"`, parentheses are re-balanced — without
    mistaking a real rewrite for one.
    """
    return re.sub(r'[\s\[\]()"]', "", expr or "").lower()


def _passed_through(source_expr: str, target_sql: str) -> bool:
    core = _core(source_expr)
    return bool(core) and core in _core(target_sql)


def _rewrite_rules(report: AssessmentReport, tables: list[TableInfo]):
    """The rewrite table, with the rules this database actually proved marked."""
    fired: dict[str, list[str]] = {}
    for f in report.findings:
        fired.setdefault(f.rule_id, []).append(f.object_name)

    texts = [o.definition for o in report.programmable_objects]
    for t in tables:
        texts.extend(d.definition for d in t.column_defaults)
        texts.extend(c.definition for c in t.check_constraints)
        texts.extend(i.filter_definition or "" for i in t.indexes)
    return rules.rewrite_rules(fired, rules.extra_rule_hits(texts))


def _gaps(
    report: AssessmentReport,
    validation: ValidationReport | None,
    parity: QueryParityReport | None,
    callables: list[CallableContract] | None = None,
) -> list[KnownGap]:
    """What did not come across. HIGH findings are grouped by rule — a wide
    database reports one rule against dozens of objects — while validation and
    parity failures are listed individually, since that detail is the point.

    The AI assessment is deliberately absent: it was produced before the plan, so
    it warns about risks the migration then handled and recommends approaches the
    migration deliberately rejected (citext, deterministic collations). In an
    artifact whose purpose is to stop a downstream agent contradicting those
    decisions, carrying it would do the opposite. It stays on the project for the
    Assessment module to show.
    """
    out: list[KnownGap] = []

    grouped: dict[str, list] = {}
    for f in report.findings:
        if f.severity is Severity.HIGH:
            grouped.setdefault(f.rule_id, []).append(f)
    for rule_id, findings in sorted(grouped.items()):
        first = findings[0]
        # TYPE_* findings recommend the assessment's ideal target (e.g. PostGIS),
        # which the migration could not use. Prefer what the column section says.
        source_type = rule_id[5:].lower() if rule_id.startswith("TYPE_") else ""
        out.append(
            KnownGap(
                id=f"assessment:{rule_id}",
                title=first.title,
                severity=first.severity.value,
                origin="assessment",
                detail=first.detail,
                recommendation=rules.TYPE_APP_IMPACT.get(source_type, first.recommendation),
                affected=sorted({f.object_name for f in findings}),
            )
        )

    if validation is not None:
        for item in validation.items:
            if item.status is MatchStatus.MATCHED:
                continue
            affected = [
                o.name for o in item.objects if o.status is not MatchStatus.MATCHED
            ]
            out.append(
                KnownGap(
                    id=f"validation:{item.id}",
                    title=f"{item.status.value.title()} in target: {item.target_name or item.source_name}",
                    severity=item.severity.value,
                    origin="validation",
                    detail=item.detail,
                    recommendation=item.recommendation,
                    affected=affected,
                )
            )

    # A procedure that returned rows and is still a Postgres procedure. Listed per
    # object rather than grouped: each one is a specific call site that fails on every
    # request, and the reader has to know which.
    for call in callables or []:
        if not call.source_returns_result_set or call.target_kind != callable_shape.PROCEDURE:
            continue
        out.append(
            KnownGap(
                id=f"callable_shape:{call.source}",
                title=f"Procedure returns a result set but was created as a PROCEDURE: {call.source}",
                severity="high",
                origin="plan",
                detail=(
                    f"{call.source} ends with a SELECT, so its caller reads rows back. In the "
                    f"target it is a PROCEDURE, and a Postgres procedure cannot return a result "
                    f"set: `SELECT * FROM {call.target}(...)` fails with SQLSTATE 42809 "
                    f'("is a procedure") and `CALL {call.target}(...)` runs it but hands the '
                    "caller nothing. No application-side change recovers the rows."
                ),
                recommendation=(
                    f"Fix it in the database, not the application: re-translate {call.source} as "
                    "`CREATE FUNCTION ... RETURNS TABLE(...)` with the final SELECT as a "
                    f"`RETURN QUERY`, then call it as `SELECT * FROM {call.target}(...)`. "
                    "Re-export this bundle afterwards so the call site here matches."
                ),
                affected=[call.target],
            )
        )

    if parity is not None:
        for comparison in parity.comparisons:
            if comparison.status.value == "match":
                continue
            out.append(
                KnownGap(
                    id=f"parity:{comparison.query.id}",
                    title=f"Query parity {comparison.status.value}: {comparison.query.title}",
                    severity="medium" if comparison.status.value == "mismatch" else "low",
                    origin="parity",
                    detail=comparison.detail,
                    recommendation=(
                        "A behavioural difference proven by running the same intent on both "
                        "sides — expect application queries of this shape to differ too."
                    ),
                    affected=comparison.mismatch_columns,
                )
            )

    return out


def _source_summary(report: AssessmentReport, project: Project) -> SourceSummary:
    return SourceSummary(
        table_count=report.table_count,
        total_rows=report.total_rows,
        programmable_object_count=report.programmable_object_count,
        tables_selected_for_data=len(project.selection),
        readiness_score=report.readiness_score,
        severity_counts=dict(report.severity_counts),
        score_formula=(
            "100 minus 10 per high, 4 per medium, 1 per low finding, clamped to "
            "0-100. The clamp means a database with many low/medium findings and no "
            "high ones can still read 0 — use severity_counts, not the score alone."
        ),
    )


def _fresh_notes(ai_notes: AiNotes | None, plan: list[PlanItem]) -> tuple[AiNotes | None, int]:
    """``ai_notes`` with every note that no longer describes the current SQL removed.

    Notes are generated once and stored on the project, so re-translating an object
    leaves its note describing SQL that is gone. That is not a harmless staleness: a
    note saying "CALL it and FETCH two refcursors" beside a deterministic call site
    saying "SELECT * FROM it" is two incompatible call forms again, which is the exact
    failure this bundle exists to prevent. A note that cannot be shown to match is
    dropped, and the count is surfaced in ``completeness`` so the fix (re-run the
    notes) is obvious.

    Notes stored before digests existed carry "" and are kept: they are unverifiable
    rather than known-stale, and silently emptying the section for every existing
    project would trade one wrong impression for another.
    """
    if ai_notes is None or not ai_notes.notes:
        return ai_notes, 0
    current = {
        item.id.split(":", 1)[-1]: sql_digest(item.sql)
        for item in plan
        if item.kind in _CODE_KINDS
    }
    kept = [
        note for note in ai_notes.notes
        if not note.sql_digest or current.get(note.source) == note.sql_digest
    ]
    dropped = len(ai_notes.notes) - len(kept)
    if not dropped:
        return ai_notes, 0
    return ai_notes.model_copy(update={"notes": kept, "stale_dropped": dropped}), dropped


def _completeness(
    project: Project,
    report: AssessmentReport | None,
    plan: list[PlanItem],
    validation: ValidationReport | None,
    parity: QueryParityReport | None,
    *,
    stale_notes: int = 0,
) -> list[str]:
    """What this bundle cannot vouch for. A bundle can be exported mid-migration,
    and silence would read as success."""
    out: list[str] = []
    if report is None:
        out.append(
            "No assessment is stored for this project, so this bundle carries only "
            "connection and static guidance — no schema, column or object detail."
        )
        return out
    if not plan:
        out.append(
            "No migration plan was built, so translated SQL, callables and "
            "expression changes are absent."
        )
    if validation is None:
        out.append(
            "Validation has not been run: object coverage, row counts and structure "
            "in the target are unverified by this bundle."
        )
    elif validation.match_score < 100:
        out.append(
            f"Validation matched {validation.match_score}% of compared objects — the "
            "unmatched ones are listed under what did not come across."
        )
    if parity is None:
        out.append(
            "Query parity has not been run: no behavioural differences have been "
            "proven by execution, so none are listed."
        )
    if not project.selection:
        out.append(
            "No tables were selected for the data load, so this bundle describes "
            "schema and object changes only."
        )
    if stale_notes:
        out.append(
            f"{stale_notes} model note{'' if stale_notes == 1 else 's'} described a "
            "translation that has since been replaced and were left out. Re-run the "
            "notes to get advice that matches the objects as they are now."
        )
    return out


def _sections(bundle_parts: dict, omitted: dict[str, int]) -> list[SectionIndex]:
    callables = bundle_parts["callables"]
    gaps = bundle_parts["gaps"]
    call_provenance = "deterministic"
    if callables:
        kinds = {c.provenance for c in callables}
        call_provenance = kinds.pop() if len(kinds) == 1 else "mixed"
    return [
        SectionIndex(
            name="target", count=1,
            summary="Driver, credentials, schema and identifier casing the application depends on.",
        ),
        SectionIndex(
            name="names", count=len(bundle_parts["names"]),
            summary="Renamed schemas, tables, primary keys and unique indexes.",
        ),
        SectionIndex(
            name="columns", count=len(bundle_parts["columns"]),
            omitted=omitted.get("columns", 0),
            summary="Columns whose value semantics or comparison behaviour changed; "
                    "columns that round-trip unchanged are omitted.",
        ),
        SectionIndex(
            name="callables", count=len(callables), provenance=call_provenance,
            summary="Procedures, functions, views and triggers, and how call sites change.",
        ),
        SectionIndex(
            name="expressions", count=len(bundle_parts["expressions"]),
            omitted=omitted.get("expressions", 0),
            summary="DEFAULT, CHECK and filtered-index predicates needing review, "
                    "before and after; cleanly translated ones are omitted.",
        ),
        SectionIndex(
            name="rewrite_rules", count=len(bundle_parts["rewrite_rules"]),
            summary="T-SQL constructs in application code and their Postgres form.",
        ),
        SectionIndex(
            name="gaps", count=len(gaps),
            summary="What did not come across, or came across differently.",
        ),
        SectionIndex(
            name="operational", count=1,
            summary="Retry, identity and naming behaviour, plus the deliberate trades.",
        ),
    ] + ([
        SectionIndex(
            name="ai_notes", count=len(bundle_parts["ai_notes"].notes),
            provenance="ai",
            summary="A model's reading of each translated object, for its callers — "
                    "advisory, and the only section not derived deterministically.",
        ),
    ] if bundle_parts.get("ai_notes") else [])


def _rewrote_scratch(report: AssessmentReport | None) -> bool:
    """Whether any `#temp` / `@t TABLE` in the source was rewritten.

    The rewrite *rules* ship either way — application code can use a construct the
    database objects never did — but the trade is about a decision, so it needs one
    to have been made.
    """
    rule_ids = set(KIND_RULE_IDS.values())
    return bool(report) and any(f.rule_id in rule_ids for f in report.findings)


def _operational(
    columns: Iterable[ColumnContract], *, rewrote_scratch: bool
) -> OperationalContract:
    """Static runtime guidance, with the trades that actually apply.

    A trade ships only when this migration really made that decision: the collation
    trade needs a nondeterministic collation to have been emitted, and the
    scratch-collection trade needs a temp table or table variable to have been
    rewritten. Otherwise it is advice about a decision that was never made.
    """
    trades = list(rules.DELIBERATE_TRADES)
    if not any(c.rejects_pattern_match for c in columns):
        trades = [t for t in trades if "Collations are mirrored" not in t]
    if not rewrote_scratch:
        trades = [t for t in trades if "Scratch collections" not in t]
    return OperationalContract(
        transient_sqlstates=rules.TRANSIENT_SQLSTATE_LIST,
        retry_note=rules.RETRY_NOTE,
        identity_note=rules.IDENTITY_NOTE,
        deliberate_trades=trades,
        notes=list(rules.OPERATIONAL_NOTES),
    )


# --- Entry point ------------------------------------------------------------------


def build_bundle(
    project: Project, *, tool_version: str = "", ai_notes: AiNotes | None = None
) -> ContextBundle:
    """Render the app-migration context bundle for ``project``.

    Still pure: ``ai_notes`` is produced by the caller (``context_bundle.ai_notes``,
    which does call a model) and passed in, so building a bundle never reaches the
    network and stays reproducible from stored state.

    Carries no connection coordinates and no secret values: the artifact is meant
    to travel to another repo or agent, so naming the workspace in it would be
    disclosure with nothing gained.
    """
    report = _assessment(project)
    plan = _plan(project)
    validation = _validation(project)
    parity = _parity(project)

    tables = _target_tables(report, plan) if report else []
    columns = _columns(tables, project) if report else []
    expressions, expressions_omitted = _expressions(tables, plan)
    # Built before `parts` because the gaps read it: a callable whose target shape
    # cannot serve its caller is a gap, and that is decided from the translated SQL.
    callables = _callables(plan, project, validation)
    parts = {
        "names": _names(report, tables, project) if report else [],
        "columns": columns,
        "callables": callables,
        "expressions": expressions,
        "rewrite_rules": _rewrite_rules(report, tables) if report else [],
        "gaps": _gaps(report, validation, parity, callables) if report else [],
    }
    # Stale notes are removed before anything renders them: a note describing a
    # translation that has since been replaced contradicts the call-site section.
    ai_notes, stale_notes = _fresh_notes(ai_notes, plan)
    parts["ai_notes"] = ai_notes
    omitted = {
        "columns": sum(len(t.columns) for t in tables) - len(columns),
        "expressions": expressions_omitted,
    }

    statuses = {k: (v.value if hasattr(v, "value") else str(v)) for k, v in project.statuses.items()}
    provenance = Provenance(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        project_id=project.id,
        bundle_version=BUNDLE_VERSION,
        tool_version=tool_version,
        phase_statuses=statuses,
        completeness=_completeness(
            project, report, plan, validation, parity, stale_notes=stale_notes
        ),
    )

    summary = (
        _source_summary(report, project)
        if report
        else SourceSummary(tables_selected_for_data=len(project.selection))
    )

    return ContextBundle(
        start_here=rules.START_HERE,
        provenance=provenance,
        sections=_sections(parts, omitted),
        source_summary=summary,
        target=_target(project),
        change_glossary=rules.change_glossary({c for col in columns for c in col.changes}),
        names=parts["names"],
        columns=columns,
        callables=parts["callables"],
        expressions=expressions,
        rewrite_rules=parts["rewrite_rules"],
        gaps=parts["gaps"],
        operational=_operational(columns, rewrote_scratch=_rewrote_scratch(report)),
        ai_notes=ai_notes,
    )
