"""What a callable is on each side of the migration: whether the source hands rows
to its caller, and what the translation actually created.

A T-SQL procedure that ends in a bare `SELECT` gives its caller a result set. A
Postgres procedure cannot do that at all, so such a procedure has to become a
function, and its call sites change from `EXEC` to `SELECT * FROM`, never `CALL`.
That makes the *shape* of a callable a migration fact in its own right, separate
from its name and its body — and one that nothing else in the pipeline established:
the object type was taken from the source and the produced SQL was never read.

Getting it wrong is not a subtle defect. Postgres rejects a mismatched call
outright, so every request through that call site fails::

    42809: public.<name>(...) is a procedure          -- SELECT * FROM a procedure
    42809: public.<name>(...) is not a procedure      -- CALL a function

Which means guidance about a call site must *state* one form. Offering two — "use
`CALL`, unless the application reads rows back, in which case it needs a function
and `SELECT * FROM`" — leaves whoever migrates the application to guess, and half
the guesses are a total outage of that feature.

Hence two signals, with deliberately different confidence:

  * ``target_kind`` is read from the translated SQL and is therefore **certain**.
    It decides the call form, which is the part that must never be wrong.
  * ``returns_result_set`` is a heuristic over T-SQL and only ever *adds* a
    warning — that the target's shape cannot serve its caller and the object needs
    re-translating as a function. A false positive costs a spurious note, not a
    broken call site.

Text analysis, not a parser, in the manner of assessment/temp_objects.
"""
from __future__ import annotations

import re

# --- What the translation produced ------------------------------------------------

_CREATE_CALLABLE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?(PROCEDURE|FUNCTION|VIEW|TRIGGER)\b",
    re.IGNORECASE,
)

# RETURNS TABLE(...) / RETURNS SETOF x — a function the caller selects rows from.
_RETURNS_SET = re.compile(r"\bRETURNS\s+(?:TABLE\b|SETOF\b)", re.IGNORECASE)
_RETURNS_REFCURSOR = re.compile(r"\brefcursor\b", re.IGNORECASE)

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_LITERAL = re.compile(r"'(?:[^']|'')*'")
# $$ ... $$ or $tag$ ... $tag$ — a PL/pgSQL body, which is prose as far as shape goes.
_DOLLAR_QUOTED = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)

PROCEDURE = "procedure"
FUNCTION = "function"
VIEW = "view"
TRIGGER = "trigger"


def statements_only(sql: str) -> str:
    """``sql`` with comments and dollar-quoted bodies blanked, offsets preserved.

    Everything that reads the *shape* of a translation has to look at its statements,
    not at the prose and PL/pgSQL inside them. A function whose body carries
    ``-- Was: CREATE PROCEDURE dbo.usp_X`` otherwise reads as a procedure, which
    reported a correctly migrated object as both the wrong kind and duplicated — two
    false alarms from one misread token.

    Comments go first: one may contain a ``$$``, and removing it keeps the real
    dollar-quote pair intact.
    """
    out = _blank(sql or "", _COMMENT)
    return _blank(out, _DOLLAR_QUOTED)


def target_kind(target_sql: str) -> str:
    """The kind of object the application calls, or "" when nothing was created.

    The *last* callable created wins. Helpers are conventionally created first, so
    this picks the main object in every shape the translator emits: a trigger's
    companion function then the trigger itself, or a procedure preceded by a helper
    function.
    """
    kinds = [m.group(1).lower() for m in _CREATE_CALLABLE.finditer(statements_only(target_sql))]
    return kinds[-1] if kinds else ""


def returns_set(target_sql: str) -> bool:
    """Whether the translated function returns rows (`RETURNS TABLE` / `SETOF`)."""
    return bool(_RETURNS_SET.search(statements_only(target_sql)))


def uses_refcursor(target_sql: str) -> bool:
    """Whether the translation hands its rows back through a refcursor.

    Worth separating: the caller has to be in an open transaction and FETCH the
    cursor by name, which is a different call site again from `SELECT * FROM`. Read
    from the signature only — a cursor the caller can FETCH has to be a parameter,
    and one merely declared inside the body is invisible to it.
    """
    return bool(_RETURNS_REFCURSOR.search(statements_only(target_sql)))


# Every object a statement creates, so "what did the migration make" is answerable
# from the plan instead of guessed from a name. Covers the helpers a translation adds
# alongside its main object: a working table for a temp collection, a composite type
# for an array rewrite, a trigger's companion function, a per-result-set function.
_CREATED = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:(?:GLOBAL|LOCAL|TEMP|TEMPORARY|UNLOGGED)\s+)*"
    r"(?:MATERIALIZED\s+VIEW|TABLE|VIEW|FUNCTION|PROCEDURE|TRIGGER|TYPE|SEQUENCE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(\"[^\"]+\"|[A-Za-z_]\w*)\s*\.\s*)?"
    r"(\"[^\"]+\"|[A-Za-z_]\w*)",
    re.IGNORECASE,
)


def identifier(raw: str | None) -> str:
    """The catalog spelling of an identifier: quoted keeps its case, bare folds."""
    if not raw:
        return ""
    return raw[1:-1] if raw.startswith('"') else raw.lower()


def created_objects(sql: str, default_schema: str = "public") -> set[tuple[str, str]]:
    """``(schema, name)`` for every object this SQL creates, as the catalog spells it.

    Read from statements only, so a name mentioned in a comment or inside a PL/pgSQL
    body is not mistaken for something that exists. Trigger names are returned
    unqualified-but-schema'd like the rest; a trigger lives on its table, so its
    "schema" is only meaningful for matching against an inventory keyed the same way.
    """
    out: set[tuple[str, str]] = set()
    for m in _CREATED.finditer(statements_only(sql)):
        name = identifier(m.group(2))
        if name:
            out.add((identifier(m.group(1)) or default_schema, name))
    return out


# --- Whether the source hands rows to its caller ----------------------------------

# A SELECT that assigns to variables returns nothing to the caller.
_ASSIGNS_VARIABLE = re.compile(
    r"\ASELECT\s+(?:DISTINCT\s+|TOP\s*\(?\s*\d+\s*\)?\s+)*@\w+\s*=", re.IGNORECASE
)
# `SELECT ... INTO #t` materialises a table instead of returning rows.
_SELECT_INTO = re.compile(r"\bINTO\s+[#@\[\w]", re.IGNORECASE)

# Text that may precede a statement-initial SELECT. T-SQL makes semicolons optional,
# so block keywords count as boundaries too.
_BOUNDARY = re.compile(r"(?:;|\bBEGIN\b|\bELSE\b)\s*\Z", re.IGNORECASE)


def _blank(text: str, pattern: re.Pattern[str]) -> str:
    """Replace every match with spaces, keeping offsets intact."""
    out = list(text)
    for m in pattern.finditer(text):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def _depths(text: str) -> list[int]:
    """Parenthesis depth at each offset, so subquery SELECTs can be skipped."""
    out, depth = [], 0
    for ch in text:
        if ch == "(":
            depth += 1
            out.append(depth)
        elif ch == ")":
            out.append(depth)
            depth = max(0, depth - 1)
        else:
            out.append(depth)
    return out


def returns_result_set(definition: str) -> bool:
    """Whether this T-SQL body hands a result set back to whoever called it.

    True for a statement-initial `SELECT` at parenthesis depth 0 that is neither a
    variable assignment nor a `SELECT ... INTO`. That excludes the shapes that look
    like a query but return nothing: `INSERT ... SELECT` (preceded by the column
    list, so not statement-initial), subqueries and `IF EXISTS (SELECT ...)` (depth
    > 0), `SELECT @x = ...`, and `SELECT ... INTO #t`.

    Biased towards precision: a missed result set leaves today's behaviour alone,
    while a false one would claim a procedure needs reshaping when it does not.
    """
    body = _blank(_blank(definition or "", _COMMENT), _LITERAL)
    depth = _depths(body)

    for m in re.finditer(r"\bSELECT\b", body, re.IGNORECASE):
        if depth[m.start()] != 0:
            continue  # a subquery, CROSS APPLY, or IF EXISTS (...)
        before = body[: m.start()]
        if before.strip() and not _BOUNDARY.search(before):
            continue  # continuation of another statement (INSERT ... SELECT, UNION)
        end = _statement_end(body, m.end(), depth)
        statement = body[m.start() : end]
        if _ASSIGNS_VARIABLE.match(statement) or _SELECT_INTO.search(statement):
            continue
        return True
    return False


def _statement_end(body: str, start: int, depth: list[int]) -> int:
    """Offset of the depth-0 `;` ending this statement, or the end of the body."""
    for i in range(start, len(body)):
        if body[i] == ";" and depth[i] == 0:
            return i
    return len(body)


# --- The mismatch -----------------------------------------------------------------


def serves_its_caller(source_type: str, definition: str, target_sql: str) -> bool:
    """Whether the translated object can still give its caller what it gave before.

    False only in the case that cannot be worked around from application code: the
    source handed rows back and the target is a PROCEDURE, which in Postgres cannot
    return a result set at all.
    """
    if source_type.upper() != "PROCEDURE" or not (target_sql or "").strip():
        return True
    if target_kind(target_sql) != PROCEDURE:
        return True
    if uses_refcursor(target_sql):
        return True  # rows come back through a cursor the caller FETCHes
    return not returns_result_set(definition)


def expected_kind(source_type: str, definition: str, target_sql: str = "") -> str:
    """The kind this object has to be in the target for its callers to still work.

    A procedure whose caller reads rows must be a function, because a Postgres
    procedure cannot return a result set. Everything else keeps its kind.

    Two independent indications that a procedure hands rows back, unioned because
    each catches what the other misses:

      * ``returns_result_set`` over the source T-SQL. Independent of the migration, so
        it still flags an object the translation got wrong — but it is a text
        heuristic, and it misses a result set fronted by a CTE, one in a body written
        without semicolons, and rows produced by EXEC-ing another procedure.
      * ``RETURNS TABLE``/``SETOF`` in the translation. The model read the whole body
        and reached the same conclusion, which covers exactly those cases. It cannot
        replace the source check — a translation is what is being verified — but as a
        second trigger it turns a silent miss into a correct expectation.

    A procedure handing rows back through `INOUT refcursor` parameters is a working
    alternative that neither signal sees; the caller checks for it separately
    (validation/comparator).
    """
    kind = (source_type or "").lower()
    if kind == PROCEDURE and (returns_result_set(definition) or returns_set(target_sql)):
        return FUNCTION
    return kind


SHAPE_REGRESSION = (
    "This procedure ends with a SELECT, so its caller reads a result set — and this "
    "translation is a Postgres PROCEDURE, which cannot return one. `SELECT * FROM "
    "<name>(...)` fails with SQLSTATE 42809 (\"is a procedure\") and `CALL <name>(...)` "
    "runs it but hands the caller nothing, so no application-side change can recover "
    "the rows. Re-translate it as CREATE FUNCTION ... RETURNS TABLE(...) with the final "
    "SELECT as a RETURN QUERY, and call it as SELECT * FROM <name>(...)."
)


def shape_regression(source_type: str, definition: str, target_sql: str) -> str:
    """``SHAPE_REGRESSION`` when the translation cannot serve its caller, else ""."""
    if serves_its_caller(source_type, definition, target_sql):
        return ""
    return SHAPE_REGRESSION
