"""App-migration context bundle — the contract handed to downstream apps/agents.

Migrating the database is only half the work: the application that talks to it
(driver, connection config, embedded T-SQL, stored-procedure calls, ORM mappings)
has to move too. This is the artifact that carries what the migration learned to
whoever does that — a human, a CI job, or another AI agent.

Two rules shape the contract:

  * It is a **delta**, not a state dump. Only what changed in the database's
    contract with application code is emitted — a column that round-trips
    identically is noise. For scale: a raw project row is ~256 KB (~64k tokens)
    for 17 tables, most of it source and target SQL the consumer cannot use.
  * It is **self-contained and self-describing**. The consumer usually cannot
    reach this app's API, so findings are embedded rather than referenced by run
    id, and ``start_here`` tells a reader what the artifact is and in what order
    to apply it.

JSON is the only format: prose is derivable from this model by whoever reads it,
the reverse is not. The frontend types in ``frontend/src/api.ts`` mirror these
1:1 — keep the two in sync.
"""
from __future__ import annotations

import hashlib

from pydantic import BaseModel

# Bumped when a field changes meaning, so a consumer can tell what it is holding.
BUNDLE_VERSION = "1"


def sql_digest(sql: str) -> str:
    """Short stable digest of a translated object's SQL.

    Lives here rather than with the notes generator so the builder can check a note
    against the current plan without importing the Foundation Model stack.
    """
    return hashlib.sha256((sql or "").strip().encode()).hexdigest()[:16]


class Provenance(BaseModel):
    """Where the bundle came from, and how much of the migration it saw.

    ``phase_statuses`` and ``completeness`` exist because a bundle can be
    exported at any phase: without them a bundle rendered before validation ran
    reads exactly like one taken after a clean validation.
    """

    generated_at: str
    project_id: str
    bundle_version: str = BUNDLE_VERSION
    tool: str = "lakebase-express"
    tool_version: str = ""
    # Phase -> not_started|in_progress|done, as recorded on the project.
    phase_statuses: dict[str, str] = {}
    # Warnings about what this bundle cannot vouch for, in plain words.
    completeness: list[str] = []


class SectionIndex(BaseModel):
    """One entry per section, so a reader sees the shape before the body.

    ``provenance`` is what produced the section: ``deterministic`` (rule engine,
    type/collation/expression mappers), ``ai`` (Foundation Model output),
    ``user-edited`` (SQL a user changed in the plan), or ``mixed``.
    """

    name: str
    count: int
    # Entries the delta filter left out (unchanged columns, cleanly translated
    # expressions), so a reader can tell a filtered section from an empty one.
    omitted: int = 0
    provenance: str = "deterministic"
    summary: str = ""


class TargetContract(BaseModel):
    """Where objects live now, and how their names must be written.

    Deliberately not a connection recipe: how the *migration tool* connected
    (its driver, its credential flow) says nothing about how the application
    should, and coordinates are infrastructure disclosure with no place in an
    artifact that travels.
    """

    target_schema: str
    identifier_case: str
    # What the case policy means for every identifier in application code.
    quoting_rule: str = ""
    notes: list[str] = []


class NameChange(BaseModel):
    """A source object whose target name differs. Unchanged names are omitted."""

    kind: str                  # schema | table | index | constraint | trigger_function
    source: str
    target: str
    note: str = ""


class ColumnContract(BaseModel):
    """A column whose contract with application code changed.

    Emitted only when something observable changed — the type's semantics, or the
    way the column compares. A column that round-trips identically never appears,
    which is what keeps this section bounded on a large database.
    """

    table: str                 # target table, schema-qualified
    source_table: str
    column: str
    source_type: str
    target_type: str
    # Keys into ContextBundle.change_glossary, which explains each change once
    # instead of repeating a paragraph on every column that shares it.
    changes: list[str] = []
    # Mirrored collation, when the column carries one.
    collation: str | None = None
    deterministic: bool = True
    # Postgres refuses LIKE/regex on a nondeterministic collation — a runtime
    # failure in application queries that nothing else warns about.
    rejects_pattern_match: bool = False


class CallableContract(BaseModel):
    """A procedure, function, view, or trigger the application calls.

    ``object_type`` is what the *source* was; ``target_kind`` is what was actually
    created, read out of the translated SQL. They differ whenever a procedure was
    reshaped into a function, and the call form follows ``target_kind`` — calling a
    function with ``CALL``, or a procedure with ``SELECT * FROM``, fails every
    request with SQLSTATE 42809.
    """

    object_type: str           # PROCEDURE | FUNCTION | VIEW | TRIGGER (the source)
    # procedure | function | view | trigger, or "" when nothing was translated.
    target_kind: str = ""
    # The target returns rows the caller selects (RETURNS TABLE / SETOF).
    returns_set: bool = False
    # The source handed a result set to its caller, so the target has to as well.
    source_returns_result_set: bool = False
    # Validation found a routine of the *source* kind still in the target while the
    # plan created the other kind: both exist and a caller can reach the stale one,
    # so no single call form can be promised until one is dropped.
    kind_conflict: bool = False
    source: str
    target: str
    # How a call site changes, e.g. EXEC dbo.X -> CALL public.x(...).
    call_change: str = ""
    translated: bool = False
    provenance: str = "ai"     # ai | user-edited | not-translated
    # Postgres needs a companion function where SQL Server had one trigger.
    companion_function: str = ""
    note: str = ""


class ExpressionChange(BaseModel):
    """A DEFAULT, CHECK, or filtered-index predicate, before and after.

    ``passed_through`` means the translator left the source text intact — usually
    fine (it was already valid Postgres), but it is also how an unsupported
    construct reaches the target, so ``risk`` names what it found still unmapped.
    """

    item_id: str               # the plan item, e.g. default:dbo.Orders.CreatedAt
    kind: str                  # default | check | index_filter
    target_object: str         # target object this rides on
    source_expr: str
    target_expr: str
    passed_through: bool = False
    risk: str = ""


class RewriteRule(BaseModel):
    """A T-SQL construct and its Postgres form, for SQL embedded in app code.

    The same rules the migration applied to procedure bodies, so application code
    is rewritten consistently with the schema instead of contradicting it.
    ``seen_in_source`` marks the rules that actually fired on this database.
    """

    tsql: str
    postgres: str
    severity: str = "low"
    seen_in_source: bool = False
    affected_objects: list[str] = []


class KnownGap(BaseModel):
    """Something the migration did not carry over, or carried over differently."""

    id: str
    title: str
    severity: str              # info | low | medium | high
    # plan | assessment | validation | parity | ai. A new value must also be added
    # to the gaps renderer in skill.py, which drops origins it does not list.
    origin: str
    detail: str = ""
    recommendation: str = ""
    affected: list[str] = []


class OperationalContract(BaseModel):
    """Runtime behaviour the application has to adapt to, not schema shape."""

    # SQLSTATEs proven worth retrying against Lakebase; they replace the app's
    # SQL Server error-number logic.
    transient_sqlstates: list[str] = []
    retry_note: str = ""
    identity_note: str = ""
    # Deliberate trades a downstream agent must not "fix" — see rules.py.
    deliberate_trades: list[str] = []
    notes: list[str] = []


class AiObjectNote(BaseModel):
    """A model's reading of one translated object, for the caller's benefit.

    ``sql_digest`` pins the note to the translation it was written against. Notes are
    generated once and stored, so a later re-translation leaves them describing SQL
    that no longer exists — and an advisory note contradicting the deterministic call
    site is the same trap as naming two call forms at once. The builder drops a note
    whose digest no longer matches rather than rendering the contradiction.
    """

    source: str
    object_type: str
    # sha256 (first 16 hex chars) of the translated SQL this note describes. Empty on
    # notes stored before this existed, which cannot be verified either way.
    sql_digest: str = ""
    # How a call site changes beyond the mechanical rename.
    call_site: str = ""
    # Behaviour that differs even when the signature looks the same.
    behaviour: str = ""
    # The thing most likely to break quietly.
    watch_out: str = ""


class AiNotes(BaseModel):
    """Advisory notes from a Foundation Model over the translated code objects.

    The only part of the bundle a model writes. Kept separate, labelled with the
    endpoint that produced it, and fail-soft (``success=False`` rather than
    raising) so the deterministic export is never held hostage to a model call.
    """

    endpoint: str = ""
    # When the model wrote them, ISO-8601 UTC. Notes are generated once and replayed
    # on every export, so without this nothing says whether they describe the current
    # translation or one from weeks ago. Empty on notes stored before this existed.
    generated_at: str = ""
    # Notes the builder removed because they described SQL that has since changed.
    # Surfaced so a caller can prompt for a re-run instead of silently showing fewer.
    stale_dropped: int = 0
    notes: list[AiObjectNote] = []
    # Translated objects that existed, so a reader can tell "no note" from
    # "not looked at" when the prompt cap bites.
    objects_total: int = 0
    success: bool = False
    error: str | None = None


class AiNotesRunState(BaseModel):
    """Progress/result of a background notes run, polled by the UI.

    The model reads every translated object, which for a real schema runs well
    past the Databricks Apps ~120s request timeout — measured at just over four
    minutes on a 29-object database. So it runs on a daemon thread and the UI
    polls this, the same way plan builds and validation runs do.
    """

    run_id: str
    status: str = "running"          # running|success|failed
    endpoint: str = ""
    objects_total: int = 0
    notes: AiNotes | None = None
    error: str | None = None


class SourceSummary(BaseModel):
    """Scale of the source, and the readiness score with its formula.

    The formula ships with the score on purpose: the score is a penalty sum
    clamped at 0, so a database with no HIGH findings can still read 0/100 and a
    consumer given the bare number concludes the migration failed.
    """

    table_count: int = 0
    total_rows: int = 0
    programmable_object_count: int = 0
    tables_selected_for_data: int = 0
    readiness_score: int = 100
    severity_counts: dict[str, int] = {}
    score_formula: str = ""


class ContextBundle(BaseModel):
    """The exported artifact. ``start_here`` is first so a reader hits it first."""

    start_here: str
    provenance: Provenance
    sections: list[SectionIndex] = []

    source_summary: SourceSummary
    target: TargetContract
    # Explains every key used in columns[].changes — emitted keys only.
    change_glossary: dict[str, str] = {}
    names: list[NameChange] = []
    columns: list[ColumnContract] = []
    callables: list[CallableContract] = []
    expressions: list[ExpressionChange] = []
    rewrite_rules: list[RewriteRule] = []
    gaps: list[KnownGap] = []
    operational: OperationalContract
    # Present only when notes were explicitly asked for; everything else is
    # deterministic, so this is the one section a reader must verify.
    ai_notes: AiNotes | None = None
