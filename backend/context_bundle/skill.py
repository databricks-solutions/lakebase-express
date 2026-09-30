"""Renders a ContextBundle as a drop-in skill for another AI agent.

The bundle is the model; this is the artifact people actually hand over. One
self-contained ``SKILL.md`` — frontmatter, instructions, and this database's
specifics — so migrating the application is "drop this into your agent" rather
than "write a prompt around this JSON".

Two rules keep it usable as context:

  * **Grouped, not enumerated.** Changes are grouped by what has to be done
    ("these 12 columns became boolean") instead of a table row per column, so a
    200-table database still renders a skill an agent can read in one pass. Each
    group still names its columns, because "which columns reject LIKE" is the
    actionable part.
  * **Bounded.** A group lists at most ``_GROUP_CAP`` names and then says how
    many more there are and where the exhaustive list lives (the JSON bundle from
    the same project).

Ordered by blast radius: connection and identifiers break everything, a known gap
breaks one feature.
"""
from __future__ import annotations

from backend.context_bundle import rules
from backend.context_bundle.models import ColumnContract, ContextBundle

# Renames shown before the casing rule is left to speak for itself. Names inside a
# change group are never truncated: "which columns reject LIKE" is the actionable
# part, and a partial list silently under-reports the work.
_RENAME_CAP = 25

SKILL_NAME = "lakebase-app-migration"

_DESCRIPTION = (
    "Migrate an application from SQL Server/Azure SQL onto its migrated Lakebase "
    "(PostgreSQL) database - identifier and column changes, procedure call sites, "
    "T-SQL rewrites, and what did not come across. Use when changing application "
    "code, queries, ORM mappings or migrations that talk to this database."
)

# How a call site changes, stated once per target kind — that is what decides the
# call form, not what the source was.
_CALL_RULE = {
    "procedure": (
        "`EXEC dbo.Proc @a = 1` becomes `CALL <target>(...)`, and named arguments "
        "become positional or `=>` notation. OUT parameters become INOUT, read back "
        "from the CALL result. These return **no** result set: `SELECT * FROM "
        "<target>(...)` fails with SQLSTATE 42809."
    ),
    "function": (
        "Call these as functions, never with `CALL` — `CALL` on a function fails with "
        "SQLSTATE 42809. A scalar one is `SELECT <target>(a)`; one returning `TABLE` "
        "or `SETOF` is queried as `SELECT * FROM <target>(a)` rather than joined "
        "directly. Argument order and types are unchanged."
    ),
    "view": "Referenced by the name below; the columns and query shape are unchanged.",
    "trigger": (
        "Fires on its own, so no call site changes — but each one now has a companion "
        "function too (see section 1), which anything inspecting the catalog will see."
    ),
}

# Plural headings, since the target kind is lower-cased.
_KIND_HEADING = {
    "procedure": "Procedures",
    "function": "Functions",
    "view": "Views",
    "trigger": "Triggers",
}

# Headings for the change keys that are not a source type name.
_GROUP_HEADINGS = {
    rules.NONDETERMINISTIC_COLLATION: "Case/accent-insensitive columns — `LIKE` and regex now fail",
    rules.TEXT_FALLBACK: "Types with no Postgres equivalent — stored as `text`",
    rules.COLLATION_LOCALE_FALLBACK: "Collation locale not recognised — sort order may differ",
}


def _cell(text: str) -> str:
    """Escape a value for a Markdown table cell. `||` in the rewrite table would
    otherwise split it into columns."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _listed(names: list[str]) -> str:
    """Names as inline code, in full."""
    return ", ".join(f"`{n}`" for n in names)


def _group_heading(key: str, columns: list[ColumnContract]) -> str:
    if key in _GROUP_HEADINGS:
        return _GROUP_HEADINGS[key]
    targets = sorted({c.target_type for c in columns})
    if targets == [key]:
        return f"`{key}` — same type name, different capabilities"
    shown = ", ".join(f"`{t}`" for t in targets[:3])
    if len(targets) > 3:
        shown += ", …"
    return f"`{key}` → {shown}"


def render_skill(bundle: ContextBundle) -> str:
    """The full ``SKILL.md`` for a project's bundle."""
    out: list[str] = []
    w = out.append

    w("---")
    w(f"name: {SKILL_NAME}")
    w(f'description: "{_DESCRIPTION}"')
    w("---")
    w("")
    w("# Migrating your application to Lakebase")
    w("")
    w(
        "This database was migrated from SQL Server/Azure SQL to Databricks Lakebase "
        "(PostgreSQL). The application that talks to it has to change too. Everything "
        "below describes *this* migration, not Postgres in general."
    )
    w("")
    w("**How to use this:** work through the sections in order — the early ones break "
      "every call site, the later ones break one feature. Anything not mentioned here "
      "round-trips unchanged, so treat silence as \"no change needed\". Never undo "
      "anything in *Do not \"fix\" these*.")
    w("")

    _completeness(w, bundle)
    _identifiers(w, bundle)
    _columns(w, bundle)
    _callables(w, bundle)
    _rewrites(w, bundle)
    _gaps(w, bundle)
    _operational(w, bundle)
    _trades(w, bundle)
    _ai_notes(w, bundle)
    _provenance(w, bundle)

    return "\n".join(out).rstrip() + "\n"


def _completeness(w, bundle: ContextBundle) -> None:
    if not bundle.provenance.completeness:
        return
    w("> [!WARNING]")
    w("> **What this skill cannot vouch for.** It was exported from a migration that "
      "had not finished every phase:")
    for note in bundle.provenance.completeness:
        w(f"> - {note}")
    w("")


def _identifiers(w, bundle: ContextBundle) -> None:
    w("## 1. Identifiers and quoting")
    w("")
    w(f"Objects live in the `{bundle.target.target_schema}` schema unless a name below "
      "says otherwise.")
    w("")
    w(f"**{bundle.target.quoting_rule}**")
    w("")
    for note in bundle.target.notes:
        w(f"- {note}")
    w("")

    renames = [n for n in bundle.names if n.kind in {"schema", "table"}]
    if renames:
        w(f"{len(renames)} schema/table names changed:")
        w("")
        w("| source | target |")
        w("|---|---|")
        for change in renames[:_RENAME_CAP]:
            w(f"| `{_cell(change.source)}` | `{_cell(change.target)}` |")
        w("")
        if len(renames) > _RENAME_CAP:
            w(f"…and {len(renames) - _RENAME_CAP} more, every one following the same rule, "
              "so apply the rule rather than looking each one up.")
            w("")

    keys = [n for n in bundle.names if n.kind == "constraint"]
    uniques = [n for n in bundle.names if n.kind == "index"]
    companions = [n for n in bundle.names if n.kind == "trigger_function"]
    if keys or uniques or companions:
        w("### Constraint and index names you may reference")
        w("")
        if keys:
            w(f"- **Primary keys** were renamed to `pk_<table>` ({len(keys)} of them); the "
              "source name was not carried over. Code using `ON CONFLICT ON CONSTRAINT` "
              "or parsing constraint names from errors must use the new name.")
        if uniques:
            w(f"- **Unique indexes** ({len(uniques)}) are named `<table>_<index>`: "
              f"{_listed([n.target for n in uniques])}")
        if companions:
            w(f"- **Triggers** each gained a companion function: "
              f"{_listed([n.target for n in companions])}")
        w("")


def _columns(w, bundle: ContextBundle) -> None:
    w("## 2. Columns that change how your code must read or compare values")
    w("")
    if not bundle.columns:
        w("No column changed in a way application code can observe.")
        w("")
        return

    section = next((s for s in bundle.sections if s.name == "columns"), None)
    if section and section.omitted:
        w(f"{section.count} of {section.count + section.omitted} columns changed "
          "observably; the rest round-trip unchanged and are not listed.")
        w("")

    groups: dict[str, list[ColumnContract]] = {}
    for column in bundle.columns:
        for key in column.changes:
            groups.setdefault(key, []).append(column)

    # Widest blast radius first.
    for key, columns in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        w(f"### {_group_heading(key, columns)} — {len(columns)} column(s)")
        w("")
        explanation = bundle.change_glossary.get(key)
        if explanation:
            w(explanation)
            w("")
        w(f"Affected: {_listed([f'{c.table}.{c.column}' for c in columns])}")
        w("")


def _callables(w, bundle: ContextBundle) -> None:
    w("## 3. Procedure, function, view and trigger call sites")
    w("")
    if not bundle.callables:
        w("The source had no programmable objects, so no call site changes.")
        w("")
        return

    # Grouped by what each object became; an untranslated one keeps its source kind.
    by_kind: dict[str, list] = {}
    for call in bundle.callables:
        by_kind.setdefault(call.target_kind or call.object_type.lower(), []).append(call)

    for kind in ("procedure", "function", "view", "trigger"):
        calls = by_kind.get(kind)
        if not calls:
            continue
        w(f"### {_KIND_HEADING[kind]} in the target — {len(calls)}")
        w("")
        w(_CALL_RULE[kind])
        w("")
        reshaped = [c for c in calls if c.object_type.lower() != kind]
        if reshaped:
            w("> [!IMPORTANT]")
            w(f"> {_listed([c.source for c in reshaped])} "
              f"{'was' if len(reshaped) == 1 else 'were'} a "
              f"{reshaped[0].object_type.lower()} in SQL Server and "
              f"{'is' if len(reshaped) == 1 else 'are'} a **{kind}** here. Use the "
              f"{kind} call form above, not the one the source implies.")
            w("")
        w("| source | now | call |")
        w("|---|---|---|")
        for call in calls:
            # An untranslated object has no target kind, so it is grouped under its
            # source kind — but nothing was created, and naming a call form for it sends
            # the caller at an object that does not exist (SQLSTATE 42883).
            if not call.translated:
                how = "not translated — no object to call"
            elif kind == "function":
                how = (f"SELECT * FROM {call.target}(...)" if call.returns_set
                       else f"SELECT {call.target}(...)")
            elif kind == "procedure":
                how = f"CALL {call.target}(...)"
            else:
                how = call.target
            w(f"| `{_cell(call.source)}` | `{_cell(call.target)}` | `{_cell(how)}` |")
        w("")

    conflicted = [c for c in bundle.callables if c.kind_conflict]
    if conflicted:
        w("> [!CAUTION]")
        w(f"> {_listed([c.source for c in conflicted])} exist in the target **twice** — "
          "once as a procedure and once as a function, because `CREATE OR REPLACE` "
          "cannot convert one kind into the other. PostgreSQL chooses between them by "
          "argument type, so a call can reach the stale one and fail with SQLSTATE "
          "42809. Do not pick a call form for these until the stale routine is dropped "
          "(see the known gaps).")
        w("")

    broken = [c for c in bundle.callables if c.source_returns_result_set
              and c.target_kind == "procedure"]
    if broken:
        one = len(broken) == 1
        w("> [!CAUTION]")
        w(f"> {_listed([c.source for c in broken])} returned a result set in SQL Server "
          f"but {'exists' if one else 'exist'} here as "
          f"{'a procedure' if one else 'procedures'}, which in Postgres cannot return "
          "one. Neither call form recovers the rows, so **do not** work around this in "
          "application code — it needs a database fix (see the known gaps).")
        w("")

    untranslated = [c for c in bundle.callables if not c.translated]
    if untranslated:
        w("> [!IMPORTANT]")
        w("> These were **not** translated — the application has to provide the logic "
          f"itself: {_listed([c.source for c in untranslated])}")
        w("")

    ai_translated = [c for c in bundle.callables if c.provenance == "ai"]
    if ai_translated:
        w(f"{len(ai_translated)} of these were translated by a language model. The "
          "signature and behaviour are worth verifying against the source before you "
          "rely on them.")
        w("")


def _rewrites(w, bundle: ContextBundle) -> None:
    w("## 4. T-SQL embedded in application code")
    w("")
    w("Rewrite it the same way the migration rewrote the database's own code, so the "
      "two do not contradict each other.")
    w("")

    seen = [r for r in bundle.rewrite_rules if r.seen_in_source]
    rest = [r for r in bundle.rewrite_rules if not r.seen_in_source]

    if seen:
        w("### Found in this database — expect it in the application too")
        w("")
        w("| T-SQL | Postgres | severity |")
        w("|---|---|---|")
        for rule in seen:
            w(f"| {_cell(rule.tsql)} | {_cell(rule.postgres)} | {rule.severity} |")
        w("")

    if rest:
        w("### Not found in the database, but still worth grepping for")
        w("")
        w("| T-SQL | Postgres |")
        w("|---|---|")
        for rule in rest:
            w(f"| {_cell(rule.tsql)} | {_cell(rule.postgres)} |")
        w("")


def _gaps(w, bundle: ContextBundle) -> None:
    w("## 5. What did not come across")
    w("")
    risky = [e for e in bundle.expressions if e.risk]
    if not bundle.gaps and not risky:
        w("Nothing outstanding was recorded.")
        w("")
        return

    # An origin missing from `order` is silently dropped, so every origin KnownGap
    # can carry needs a row here. `plan` leads: it holds call sites that fail on
    # every request, which outranks a construct someone has to rewrite by hand.
    labels = {
        "plan": "Objects whose target shape cannot serve their caller",
        "assessment": "Source constructs that need a manual rewrite",
        "validation": "Objects that do not match in the target",
        "parity": "Behavioural differences proven by running queries on both sides",
    }
    order = ["plan", "assessment", "validation", "parity"]
    by_origin: dict[str, list] = {}
    for gap in bundle.gaps:
        by_origin.setdefault(gap.origin, []).append(gap)

    for origin in order:
        gaps = by_origin.get(origin)
        if not gaps:
            continue
        w(f"### {labels.get(origin, origin)}")
        w("")
        for gap in gaps:
            w(f"- **{_cell(gap.title)}** ({gap.severity})"
              + (f" — {_cell(gap.detail)}" if gap.detail else ""))
            if gap.recommendation:
                w(f"  - What to do: {_cell(gap.recommendation)}")
            if gap.affected:
                w(f"  - Affects: {_listed(gap.affected)}")
        w("")

    if risky:
        w("### Constraints and defaults that may not be enforced")
        w("")
        w("These predicates still contained T-SQL the translator could not map, so "
          "they fail when applied. Until they are fixed the database is not enforcing "
          "them — validate in the application if you were relying on them.")
        w("")
        for change in risky:
            w(f"- **{_cell(change.target_object)}** ({change.kind}) — {_cell(change.risk)}")
            w(f"  - Source: `{_cell(change.source_expr)}`")
        w("")


def _operational(w, bundle: ContextBundle) -> None:
    op = bundle.operational
    w("## 6. Runtime behaviour")
    w("")
    w(f"**Identity columns.** {op.identity_note}")
    w("")
    w(f"**Retries.** {op.retry_note}")
    w("")
    w("Retry on these SQLSTATEs: "
      + ", ".join(f"`{code}`" for code in op.transient_sqlstates))
    w("")
    for note in op.notes:
        w(f"- {note}")
    w("")


def _trades(w, bundle: ContextBundle) -> None:
    if not bundle.operational.deliberate_trades:
        return
    w('## 7. Do not "fix" these')
    w("")
    w("Deliberate trade-offs, not defects. Each was chosen so the target behaves like "
      "the source; undoing one silently changes query results.")
    w("")
    for trade in bundle.operational.deliberate_trades:
        w(f"- {trade}")
    w("")


def _ai_notes(w, bundle: ContextBundle) -> None:
    """The one section a model wrote, labelled as such and with its endpoint named.

    Kept last on purpose: a reader should have the deterministic facts before
    anything advisory, and should be able to skip this entirely.
    """
    ai = bundle.ai_notes
    if ai is None:
        return
    w("## 8. Model notes on the translated objects (advisory)")
    w("")
    if not ai.success:
        w(f"Notes were requested but could not be produced: {_cell(ai.error or 'unknown error')}")
        w("")
        return
    # When, not just who: these are generated once and replayed on every export, so a
    # reader has no other way to tell current advice from advice about a translation
    # that has since been replaced.
    written = f" on {_cell(ai.generated_at)}" if ai.generated_at else ""
    w(f"**Advisory — written by `{ai.endpoint}`{written}, not derived from the "
      "migration.** Everything above this section is deterministic; this part is a "
      "model's reading of each object's original T-SQL beside its translation. Verify "
      "a note against the source before you act on it, and prefer the sections above "
      "where they disagree.")
    w("")
    if ai.stale_dropped:
        plural = "" if ai.stale_dropped == 1 else "s"
        w("> [!NOTE]")
        w(f"> {ai.stale_dropped} note{plural} described a translation that has since been "
          "replaced and {} left out — re-run the notes for advice that matches the "
          "objects as they are now.".format("was" if ai.stale_dropped == 1 else "were"))
        w("")
    unread = ai.objects_total - len(ai.notes)
    if unread > 0:
        w(f"Covers {len(ai.notes)} of {ai.objects_total} translated objects — the rest were "
          "not read, to keep the request inside the model's window. No note here means "
          "\"not looked at\", not \"nothing to worry about\".")
        w("")
    for note in ai.notes:
        heading = f"### `{note.source}`"
        if note.object_type:
            heading += f" ({note.object_type.title()})"
        w(heading)
        w("")
        if note.call_site:
            w(f"- **At the call site:** {_cell(note.call_site)}")
        if note.behaviour:
            w(f"- **Behaviour:** {_cell(note.behaviour)}")
        if note.watch_out:
            w(f"- **Watch out:** {_cell(note.watch_out)}")
        w("")


def _provenance(w, bundle: ContextBundle) -> None:
    p = bundle.provenance
    summary = bundle.source_summary
    w("---")
    w("")
    w(f"Generated {p.generated_at} by {p.tool} {p.tool_version} "
      f"(bundle v{p.bundle_version}).")
    w("")
    w(f"Describes a source of {summary.table_count} tables and "
      f"{summary.programmable_object_count} programmable objects.")
    w("")
    w("No connection details or secret values are included. Everything here is "
      "derived from the source scan and the migration that was applied.")
