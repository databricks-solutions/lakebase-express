"""T-SQL scratch collections (`#temp`, `##global`, `DECLARE @t TABLE`) and the
Postgres shape that replaces each one.

A `#temp` table is idiomatic, cheap scratch space in SQL Server — tempdb is built
for it — so a mechanical `#temp` -> `CREATE TEMP TABLE` translation looks like the
obvious move. On Lakebase it is a trap, for three independent reasons:

  * **The pooled endpoint has no session temp tables.** Lakebase fronts Postgres
    with PgBouncer in transaction pooling mode — fixed, not configurable — and
    session-held temporary tables are on its documented list of unsupported
    session features. A temp table that outlives the transaction that made it is
    gone, or belongs to another client's backend; scale-to-zero drops session
    state too.
  * **Creating one is not free.** Every `CREATE TEMP TABLE` writes rows into the
    shared catalogs, so a procedure on a hot OLTP path churns them on every call.
  * **The planner is blind to it.** Autovacuum cannot reach a temp table, so it
    carries no statistics unless the procedure `ANALYZE`s it by hand — and a join
    against a table the planner believes is empty is where a migrated procedure
    goes quadratic.

Which construct to use instead is not a property of the collection; it is a
property of how the body *uses* it. That is what this module reads out of the
T-SQL — how many statements fill it, how many read it, whether it is indexed or
mutated, and what feeds it — and one analysis then drives three things: the
assessment finding the user reads, the guidance in the translator's prompt, and
the guardrail that flags a translation which emitted a temp table anyway.

Text analysis, not a parser: it prefers a false positive to silence, and a
collection assembled inside dynamic SQL comes back as REVIEW rather than a guess.
Row counts are the one signal the body cannot supply, so they are passed in from
the scan when the caller has them — sizing is a decision for the user, which is
why the assessment supplies them and the translator deliberately does not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# Upper bound above which an in-memory array or an inlined CTE stops being the
# obvious choice and the rewrite becomes a design decision. Deliberately a prompt
# for a human, not a hard limit — the number it is compared against is the *source*
# table's size, so it is a ceiling the runtime WHERE usually cuts down.
LARGE_COLLECTION_ROWS = 100_000


class Strategy(str, Enum):
    """The Postgres shape that replaces one scratch collection."""

    CTE = "cte"                        # folds into the statement that reads it
    PLPGSQL = "plpgsql"                # a record or an array of a composite type
    WORKING_TABLE = "working_table"    # too big/indexed/shared for either — user decides
    REVIEW = "review"                  # usage not readable from the body


LOCAL_TEMP = "local_temp"
GLOBAL_TEMP = "global_temp"
TABLE_VARIABLE = "table_variable"

KIND_LABELS = {
    LOCAL_TEMP: "Temp table (#table)",
    GLOBAL_TEMP: "Global temp table (##table)",
    TABLE_VARIABLE: "Table variable (@table)",
}

# Which compatibility rule a kind reports under, so the assessment and the context
# bundle's rewrite table keep agreeing on rule ids.
KIND_RULE_IDS = {
    LOCAL_TEMP: "TEMP_TABLE",
    GLOBAL_TEMP: "TEMP_TABLE",
    TABLE_VARIABLE: "TABLE_VARIABLE",
}

# The reason the mechanical translation is wrong, stated once. Long, because it is
# the sentence that stops a reviewer "fixing" a CTE back into a temp table.
WHY_NOT_TEMP = (
    "Do not translate it to CREATE TEMP TABLE. Lakebase fronts Postgres with PgBouncer in "
    "transaction pooling mode — fixed, not configurable — and session-held temporary tables "
    "are on its documented list of unsupported session features: a temp table that outlives "
    "its transaction is gone, or belongs to another client's backend, and scale-to-zero drops "
    "session state as well. Even within one transaction it is not free — every CREATE TEMP "
    "TABLE writes catalog rows, so a procedure on a hot path churns the shared catalogs on "
    "every call, and autovacuum cannot reach a temp table, so the planner sees no statistics "
    "for it unless the procedure ANALYZEs it by hand."
)

REWRITES: dict[Strategy, str] = {
    Strategy.CTE: (
        "Fold it into the statement that reads it, as a CTE — `WITH stage AS (SELECT ...) "
        "SELECT ... FROM stage`. Postgres inlines a non-recursive CTE into the surrounding "
        "plan, so the planner optimises across it instead of materialising a relation it has "
        "no statistics for. Add MATERIALIZED only when the subquery is expensive and read "
        "more than once."
    ),
    Strategy.PLPGSQL: (
        "Hold it in a PL/pgSQL variable rather than a relation: `SELECT ... INTO` a record "
        "for a single row, or an array of a composite type for a set — build it with "
        "array_agg(), read it back with unnest(), iterate it with FOREACH ... IN ARRAY. A "
        "fill-then-modify-then-read sequence can also collapse into data-modifying CTEs "
        "(`WITH upd AS (UPDATE ... RETURNING *) ...`), which is the same rewrite an OUTPUT "
        "clause needs. The array lives in the backend's memory, so keep it to a few thousand "
        "rows; if it can grow past that, treat it as a working table instead."
    ),
    Strategy.WORKING_TABLE: (
        "Neither a CTE nor an array fits this one: give it a real table in the target schema "
        "(UNLOGGED, so it costs no WAL), keyed by a run id the caller passes in, and delete "
        "that key's rows when the procedure finishes. This is a design decision rather than a "
        "mechanical rewrite — confirm the realistic row count, and decide who owns the cleanup "
        "if the procedure fails partway."
    ),
    Strategy.REVIEW: (
        "How this collection is filled and read could not be determined from the body — it is "
        "most likely assembled inside dynamic SQL. Resolve the dynamic SQL first, then pick "
        "the rewrite from the same three options (CTE, PL/pgSQL variable, or a real working "
        "table)."
    ),
}

# What to call each strategy in a finding title.
STRATEGY_LABELS = {
    Strategy.CTE: "fold into a CTE",
    Strategy.PLPGSQL: "hold in a PL/pgSQL variable",
    Strategy.WORKING_TABLE: "needs a real working table — decide before migrating",
    Strategy.REVIEW: "usage unreadable — review by hand",
}


@dataclass(frozen=True)
class TempObject:
    """One scratch collection, and what the body does with it."""

    name: str                 # as written: "#stage", "##shared", "@ids"
    kind: str
    writes: int               # statements that fill it (INSERT / SELECT ... INTO)
    reads: int                # statements that read it (FROM / JOIN / APPLY)
    indexed: bool             # CREATE INDEX on it, or a key/index in its declaration
    mutated: bool             # UPDATE / DELETE / MERGE / TRUNCATE against it
    in_dynamic_sql: bool      # referenced from inside a string literal
    row_source: str = ""      # scanned table that feeds it ("" when none matched)
    max_rows: int | None = None  # row_source's row count — a ceiling, not an estimate


@dataclass(frozen=True)
class TempUsage:
    """A collection plus the decision made about it."""

    obj: TempObject
    strategy: Strategy
    reason: str               # why this strategy, as one clause for the finding

    @property
    def summary(self) -> str:
        """One line naming the collection, its rewrite, and the evidence."""
        return f"{self.obj.name} -> {STRATEGY_LABELS[self.strategy]} ({self.reason})"


# --- Reading the body -------------------------------------------------------------

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_LITERAL = re.compile(r"'(?:[^']|'')*'")

_LOCAL_NAME = re.compile(r"(?<!#)#([A-Za-z_]\w*)")
_GLOBAL_NAME = re.compile(r"##([A-Za-z_]\w*)")
_TABLE_VAR_NAME = re.compile(r"\bDECLARE\s+@([A-Za-z_]\w*)\s+TABLE\b", re.IGNORECASE)

# How far past a fill statement to look for the table feeding it.
_SOURCE_WINDOW = 1500
_SOURCE_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+((?:\[?\w+\]?\s*\.\s*)?\[?\w+\]?)", re.IGNORECASE)


def _blank(text: str, pattern: re.Pattern[str]) -> str:
    """Replace every match with spaces, keeping offsets (and newlines) intact."""
    out = list(text)
    for m in pattern.finditer(text):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def _token(name: str) -> str:
    """Regex for a reference to ``name``, not followed by more name characters."""
    return re.escape(name) + r"(?!\w)"


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.IGNORECASE))


def _declaration_body(text: str, tok: str) -> str:
    """The column list in `CREATE TABLE #t (...)` / `DECLARE @t TABLE (...)`."""
    m = re.search(rf"\b(?:CREATE\s+TABLE|DECLARE)\s+{tok}\s*(?:TABLE\s*)?\(", text, re.IGNORECASE)
    if not m:
        return ""
    open_paren = m.end() - 1
    depth = 0
    for i in range(open_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : i]
    return text[open_paren + 1 :]


def _normalise(name: str) -> str:
    return re.sub(r"[\[\]\"\s]", "", name).lower()


def _lookup_rows(name: str, row_counts: dict[str, int]) -> int | None:
    """Row count for a table named in the source, qualified or not.

    ``row_counts`` is keyed by lower-cased `schema.table`. A bare name resolves
    only when exactly one scanned schema has it — an ambiguous one is left alone
    rather than guessed at.
    """
    key = _normalise(name)
    if not key or key.startswith(("#", "@")):
        return None
    if key in row_counts:
        return row_counts[key]
    hits = [v for k, v in row_counts.items() if k.rsplit(".", 1)[-1] == key]
    return hits[0] if len(hits) == 1 else None


def _feeding_table(
    text: str, tok: str, row_counts: dict[str, int]
) -> tuple[str, int | None]:
    """The largest scanned table feeding this collection, and its row count.

    Looks only at the statements that fill the collection: the tables a *reader*
    joins it against say nothing about how many rows it holds.
    """
    best_name, best_rows = "", None
    for m in re.finditer(rf"\b(?:INSERT\s+INTO|INSERT|INTO)\s+{tok}", text, re.IGNORECASE):
        window = text[m.end() : m.end() + _SOURCE_WINDOW].split(";", 1)[0]
        for src in _SOURCE_TABLE.finditer(window):
            rows = _lookup_rows(src.group(1), row_counts)
            if rows is not None and (best_rows is None or rows > best_rows):
                best_name, best_rows = _normalise(src.group(1)), rows
    return best_name, best_rows


def find_temp_objects(
    definition: str, row_counts: dict[str, int] | None = None
) -> list[TempObject]:
    """Every scratch collection in a T-SQL body, in the order it first appears."""
    counts = row_counts or {}
    body = _blank(definition, _COMMENT)
    literal_spans = [(m.start(), m.end()) for m in _LITERAL.finditer(body)]
    # Names are looked for outside string literals, so `PRINT 'filling #stage'` does
    # not invent a collection. Usage is then counted across the whole body, because a
    # collection really built inside dynamic SQL still has to be reported.
    outside_literals = _blank(body, _LITERAL)

    named: list[tuple[str, str]] = []
    for pattern, kind, prefix in (
        (_GLOBAL_NAME, GLOBAL_TEMP, "##"),
        (_LOCAL_NAME, LOCAL_TEMP, "#"),
        (_TABLE_VAR_NAME, TABLE_VARIABLE, "@"),
    ):
        for m in pattern.finditer(outside_literals):
            entry = (f"{prefix}{m.group(1)}", kind)
            if entry not in named:
                named.append(entry)

    out: list[TempObject] = []
    for name, kind in named:
        tok = _token(name)
        refs = list(re.finditer(tok, body))
        if not refs:
            continue
        decl = _declaration_body(body, tok)
        row_source, max_rows = _feeding_table(body, tok, counts)
        out.append(
            TempObject(
                name=name,
                kind=kind,
                writes=_count(rf"\b(?:INSERT\s+INTO|INSERT|INTO)\s+{tok}", body),
                reads=_count(rf"\b(?:FROM|JOIN|APPLY)\s+{tok}", body),
                indexed=bool(
                    re.search(
                        rf"\bCREATE\s+(?:UNIQUE\s+)?(?:(?:NON)?CLUSTERED\s+)?INDEX\s+\S+\s+ON\s+{tok}",
                        body,
                        re.IGNORECASE,
                    )
                    or (decl and re.search(r"\bPRIMARY\s+KEY\b|\bUNIQUE\b|\bINDEX\b", decl, re.IGNORECASE))
                ),
                mutated=bool(
                    re.search(
                        rf"\b(?:UPDATE|DELETE\s+FROM|DELETE|MERGE\s+INTO|MERGE|TRUNCATE\s+TABLE)\s+{tok}",
                        body,
                        re.IGNORECASE,
                    )
                ),
                in_dynamic_sql=any(
                    s <= r.start() < e for r in refs for s, e in literal_spans
                ),
                row_source=row_source,
                max_rows=max_rows,
            )
        )
    return out


# --- Deciding the rewrite ---------------------------------------------------------


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _size_clause(obj: TempObject) -> str:
    """What feeds the collection, when the scan knew — evidence for a sizing call."""
    if obj.max_rows is None:
        return ""
    return f", and it is filled from {obj.row_source} (up to {obj.max_rows:,} rows)"


def classify(obj: TempObject) -> tuple[Strategy, str]:
    """The Postgres shape for one collection, and the evidence behind it.

    Ordered by how much it constrains the answer: something the target cannot
    express at all first, then what rules a CTE out, then what rules an array out.
    """
    if obj.in_dynamic_sql:
        return Strategy.REVIEW, "referenced from inside dynamic SQL"
    if obj.kind == GLOBAL_TEMP:
        return (
            Strategy.WORKING_TABLE,
            "a ## collection is shared between sessions, which no CTE, variable or "
            "Postgres temp table can express",
        )
    if obj.indexed:
        return (
            Strategy.WORKING_TABLE,
            "the source indexes it, so it is probed repeatedly — a CTE cannot carry an "
            "index and an array cannot be searched" + _size_clause(obj),
        )
    if obj.max_rows is not None and obj.max_rows >= LARGE_COLLECTION_ROWS:
        return (
            Strategy.WORKING_TABLE,
            f"filled from {obj.row_source}, up to {obj.max_rows:,} rows — too large to "
            "hold in a backend's memory as an array",
        )
    if obj.writes == 0 and obj.reads == 0:
        return Strategy.REVIEW, "declared but never visibly filled or read"
    if obj.mutated:
        return (
            Strategy.PLPGSQL,
            "modified after it is filled, which a CTE cannot do in place",
        )
    if obj.writes > 1 or obj.reads > 1:
        return (
            Strategy.PLPGSQL,
            f"filled by {_plural(obj.writes, 'statement')} and read by "
            f"{_plural(obj.reads, 'statement')} — a CTE is scoped to the one statement "
            "that declares it",
        )
    return Strategy.CTE, "filled once and read by one statement"


def analyze(
    definition: str, row_counts: dict[str, int] | None = None
) -> list[TempUsage]:
    """Every scratch collection in a T-SQL body, with its rewrite decided.

    ``row_counts`` (lower-cased `schema.table` -> rows, from the scan) is what
    lets a large collection escalate to a working table; without it the decision
    is made on shape alone, which is what the translator gets.
    """
    out: list[TempUsage] = []
    for obj in find_temp_objects(definition, row_counts):
        strategy, reason = classify(obj)
        out.append(TempUsage(obj=obj, strategy=strategy, reason=reason))
    return out


# --- Guardrail on the translated result -------------------------------------------

_CREATE_TEMP = re.compile(
    r"\bCREATE\s+(?:GLOBAL\s+|LOCAL\s+)?TEMP(?:ORARY)?\s+TABLE\b", re.IGNORECASE
)


def temp_table_regression(definition: str, target_sql: str) -> str:
    """Why the translated SQL should not have created a temp table, or "".

    A translation is not rejected over this — the SQL still applies — but a model
    that reached for CREATE TEMP TABLE anyway has produced something that breaks on
    the pooled endpoint, and the reviewer has to see that before it ships. Stays
    quiet when the analysis itself concluded a real relation was needed, since then
    the temp table is a reviewer's decision to make rather than a slip.
    """
    if not _CREATE_TEMP.search(target_sql or ""):
        return ""
    if any(u.strategy is Strategy.WORKING_TABLE for u in analyze(definition)):
        return (
            "This translation creates a TEMP TABLE, and one of the source's scratch "
            "collections does need a real relation — but a Postgres temp table is not it: "
            "Lakebase's pooled endpoint has no session temp tables. Give it an UNLOGGED "
            "table in the target schema, keyed by a run id, and clean the key up."
        )
    return (
        "This translation creates a TEMP TABLE even though every scratch collection in the "
        "source could be rewritten as a CTE or a PL/pgSQL variable. " + WHY_NOT_TEMP
    )
