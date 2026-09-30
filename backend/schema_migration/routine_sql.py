"""Deterministic preamble that makes a routine-kind change idempotent.

`CREATE OR REPLACE` replaces a routine of the *same* kind. It cannot turn a
procedure into a function, and Postgres offers no `CREATE OR REPLACE ROUTINE`. So
when a translation reshapes a procedure into a function — which any procedure that
returns a result set must be — re-applying it to a target that already holds the
old procedure goes wrong in one of two ways, and neither is loud:

  * **Same signature** — the apply fails with SQLSTATE 42P13, "cannot change
    routine kind". The target keeps the procedure.
  * **Different signature** (a parameter mapped to `varchar` rather than `text`, a
    default folded away) — Postgres happily keeps **both**. Two routines then share
    one name, and overload resolution decides which one a caller reaches. An
    application binding a string parameter lands on whichever matches `text`.

In both cases the plan says "function", the database says "procedure", and the
caller gets SQLSTATE 42809 on every request. Worse, it is invisible to everything
downstream: the migration plan records the SQL it *intended*, so a context bundle
built from it describes a function that is not there.

The fix is to drop the stale routine first. It is emitted where the SQL is
**applied** — the sync executor and the async post-load notebook — rather than only
where it is translated, for the same reason trigger_sql is: the plan stores whatever
SQL was produced, so a plan built before this existed, or hand-edited since, must
still apply cleanly.

Deliberately narrow, because it drops things:

  * only when the source object's kind and the created routine's kind actually
    differ — an ordinary procedure-to-procedure translation gets nothing;
  * only routines of the *opposite* kind (`CREATE OR REPLACE` already handles the
    same kind), in the schema and under the name being created;
  * every overload of that kind, since the stale signature is exactly what is not
    known here;
  * no CASCADE — if something depends on the stale routine, the drop fails and says
    so, which is better than quietly removing a dependency.
"""
from __future__ import annotations

import re

from backend.assessment import callable_shape

# The routine a statement creates, with its optional schema qualifier. Both parts
# may be double-quoted, which is what preserves their case.
_CREATE_ROUTINE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE)\s+"
    r"(?:(\"[^\"]+\"|[A-Za-z_]\w*)\s*\.\s*)?"
    r"(\"[^\"]+\"|[A-Za-z_]\w*)",
    re.IGNORECASE,
)

# pg_proc.prokind for the kind we are NOT creating.
_STALE_KIND = {"function": "p", "procedure": "f"}


def _identifier(raw: str | None) -> str:
    """The catalog spelling of an identifier: quoted keeps its case, bare folds."""
    if not raw:
        return ""
    if raw.startswith('"'):
        return raw[1:-1]
    return raw.lower()


def _literal(value: str) -> str:
    """``value`` as a SQL string literal body (single quotes doubled)."""
    return value.replace("'", "''")


def _dollar_tag(*embedded: str) -> str:
    """A dollar-quote tag that cannot appear in ``embedded``.

    The body of the DO block is dollar-quoted, and Postgres identifiers may legally
    contain `$` — so an object named `a$lbx_kind$b` would close the block early and
    everything after it would parse as top-level SQL. The tag is extended until it is
    absent from every value interpolated into the body, which makes that impossible
    rather than unlikely.
    """
    tag = "lbx_kind"
    while any(f"${tag}$" in value for value in embedded):
        tag += "x"
    return tag


# An identifier parsed out of a CREATE statement cannot contain these. Their presence
# means the text was mangled rather than parsed — blanking `--` comments truncates a
# quoted identifier that contains one, losing its closing quote and swallowing what
# follows. Better to skip the guard than to aim a DROP at a name that was never there.
_MANGLED_IDENT = re.compile(r"[;\n\r\x00]")


def kind_change_preamble(source_kind: str, sql: str, default_schema: str = "public") -> str:
    """SQL dropping the stale routine, or "" when no kind change is happening.

    ``source_kind`` is the source object's type (the plan item's kind); the created
    kind is read from ``sql``. A trigger or view translation returns "" — only a
    function/procedure swap can collide.
    """
    created = callable_shape.target_kind(sql)
    stale = _STALE_KIND.get(created)
    source = (source_kind or "").lower()
    if stale is None or source not in _STALE_KIND or source == created:
        return ""

    # The routine this statement creates — the last CREATE of the created kind, to
    # match how target_kind picks the main object past any helper.
    # Statements only: a name mentioned in a comment or inside a PL/pgSQL body must not
    # become the DROP target.
    matches = [
        m for m in _CREATE_ROUTINE.finditer(callable_shape.statements_only(sql))
        if m.group(1).lower() == created
    ]
    if not matches:
        return ""
    m = matches[-1]
    schema = _identifier(m.group(2)) or default_schema
    name = _identifier(m.group(3))
    if not name or _MANGLED_IDENT.search(schema) or _MANGLED_IDENT.search(name):
        return ""

    stale_word = "PROCEDURE" if stale == "p" else "FUNCTION"
    tag = _dollar_tag(schema, name)
    return (
        f"-- The source {source} became a {created}; Postgres cannot replace one kind\n"
        f"-- with the other, and leaving both would let callers reach the stale one.\n"
        f"DO ${tag}$\n"
        "DECLARE stale record;\n"
        "BEGIN\n"
        "  FOR stale IN\n"
        "    SELECT p.oid::regprocedure AS signature\n"
        "    FROM   pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace\n"
        f"    WHERE  n.nspname = '{_literal(schema)}'\n"
        f"      AND  p.proname = '{_literal(name)}'\n"
        f"      AND  p.prokind = '{stale}'\n"
        "  LOOP\n"
        f"    RAISE NOTICE 'dropping stale {stale_word.lower()} %', stale.signature;\n"
        f"    EXECUTE format('DROP {stale_word} %s', stale.signature);\n"
        "  END LOOP;\n"
        "END\n"
        f"${tag}$;\n"
    )


def with_kind_guard(source_kind: str, sql: str, default_schema: str = "public") -> str:
    """``sql`` with the stale-routine drop in front, when a kind change needs it."""
    preamble = kind_change_preamble(source_kind, sql, default_schema)
    return f"{preamble}\n{sql}" if preamble else sql
