"""Mechanical T-SQL scalar-expression -> Postgres translation.

Used for the small expressions that ride on tables — column DEFAULTs, CHECK
constraint predicates, and filtered-index WHERE clauses. These are tiny and
overwhelmingly formulaic (literals, getdate(), newid(), simple comparisons), so
they are translated deterministically — no Foundation Model round trip. The
translation is intentionally conservative: known constructs are rewritten,
everything else is left verbatim so an unsupported expression fails visibly at
apply time (each plan item applies in its own transaction) and can be edited in
the plan UI instead of silently changing meaning.
"""
from __future__ import annotations

import re

# T-SQL function calls with a direct Postgres spelling. Matched on the word
# followed by "(" so bare identifiers named e.g. "len" are left alone.
_FUNCTION_MAP: dict[str, str] = {
    "getdate": "now",
    "sysdatetime": "now",
    "current_timestamp": "now",
    "sysdatetimeoffset": "now",
    "newid": "gen_random_uuid",
    "newsequentialid": "gen_random_uuid",
    "isnull": "coalesce",
    "len": "length",
    "datalength": "octet_length",
}

# Calls that need more than a rename (different shape, not just a new name).
_CALL_REWRITES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgetutcdate\s*\(\s*\)", re.IGNORECASE), "(now() AT TIME ZONE 'utc')"),
    (re.compile(r"\bsysutcdatetime\s*\(\s*\)", re.IGNORECASE), "(now() AT TIME ZONE 'utc')"),
    (re.compile(r"\bsuser_sname\s*\(\s*\)", re.IGNORECASE), "current_user"),
    (re.compile(r"\boriginal_login\s*\(\s*\)", re.IGNORECASE), "current_user"),
    (re.compile(r"\buser_name\s*\(\s*\)", re.IGNORECASE), "current_user"),
    (re.compile(r"\bhost_name\s*\(\s*\)", re.IGNORECASE), "inet_client_addr()::text"),
]

_BRACKET_IDENT = re.compile(r"\[([^\]]+)\]")
_NSTRING = re.compile(r"\bN(')")
_FUNC_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# T-SQL sequence access: NEXT VALUE FOR [schema].[seq]  ->  nextval('schema.seq').
# Captures optional schema + sequence name (bracket- or bare-quoted).
_NEXT_VALUE_FOR = re.compile(
    r"NEXT\s+VALUE\s+FOR\s+"
    r"(?:\[?([A-Za-z_][A-Za-z0-9_$]*)\]?\s*\.\s*)?"   # optional schema
    r"\[?([A-Za-z_][A-Za-z0-9_$]*)\]?",               # sequence name
    re.IGNORECASE,
)

# A bit column's default is the integer 0/1 (often over-parenthesised as ((1)));
# the column maps to Postgres boolean, which won't take an integer default.
_BOOL_LITERAL = re.compile(r"^\(*\s*([01])\s*\)*$")


def map_expression(expr: str) -> str:
    """Translate a T-SQL scalar expression to its Postgres form.

    Handles bracket quoting ([Col] -> "Col", case preserved to match the
    scanned column names the tables keep), N'..' literals, and the common
    date/uuid/null functions. Unknown constructs pass through verbatim.
    """
    out = (expr or "").strip()

    # Protect nothing fancy: T-SQL system expressions here don't nest strings
    # with brackets in practice, so plain regex passes are sufficient.
    out = _BRACKET_IDENT.sub(r'"\1"', out)
    out = _NSTRING.sub(r"\1", out)

    for pat, repl in _CALL_REWRITES:
        out = pat.sub(repl, out)

    def _rename(m: re.Match[str]) -> str:
        fn = m.group(1)
        mapped = _FUNCTION_MAP.get(fn.lower())
        return f"{mapped}(" if mapped else m.group(0)

    out = _FUNC_CALL.sub(_rename, out)
    return out


def map_default_expression(
    expr: str, *, column=None, target_schema: str = "public", identifier_case="lowercase"
) -> str:
    """Translate a column DEFAULT expression, with two fix-ups the generic scalar
    mapper can't make without column/type context:

      * a ``bit`` column becomes Postgres ``boolean``, so its ``((1))``/``((0))``
        default is rewritten to ``true``/``false`` (an integer default is rejected);
      * ``NEXT VALUE FOR [schema].[seq]`` becomes ``nextval('schema.seq')`` with
        the sequence's schema mapped like everything else (the sequence itself is
        created by the plan's identity/sequence handling).

    ``column`` is the scanned source ColumnInfo (for the bit test); everything
    else falls through to :func:`map_expression`.
    """
    raw = (expr or "").strip()

    if column is not None and column.data_type.lower() == "bit":
        m = _BOOL_LITERAL.match(raw)
        if m:
            return "true" if m.group(1) == "1" else "false"

    # Lazy import to avoid a cycle (naming imports nothing from here, but keep it
    # local so expr_mapper stays dependency-light for the notebook text path).
    from backend.schema_migration.naming import map_object, map_schema

    def _nextval(m: re.Match[str]) -> str:
        src_schema, seq = m.group(1), m.group(2)
        schema = (
            map_schema(src_schema, target_schema, identifier_case)
            if src_schema
            else map_schema("dbo", target_schema, identifier_case)
        )
        mapped_seq = map_object(seq, identifier_case)
        if identifier_case == "preserve":
            return f'''nextval('"{schema}"."{mapped_seq}"')'''
        return f"nextval('{schema}.{mapped_seq}')"

    if _NEXT_VALUE_FOR.search(raw):
        # The sequence reference is the whole default; translate it directly
        # (strip the surrounding parens T-SQL adds).
        return _NEXT_VALUE_FOR.sub(_nextval, raw.strip("()"))

    return map_expression(raw)


def sequence_ref_in(
    expr: str, target_schema: str = "public", identifier_case="lowercase"
) -> tuple[str, str] | None:
    """If ``expr`` reads a sequence via ``NEXT VALUE FOR``, return the mapped
    ``(schema, sequence_name)`` so the caller can CREATE SEQUENCE it. Else None."""
    from backend.schema_migration.naming import map_object, map_schema

    m = _NEXT_VALUE_FOR.search(expr or "")
    if not m:
        return None
    src_schema, seq = m.group(1), m.group(2)
    schema = (
        map_schema(src_schema, target_schema, identifier_case)
        if src_schema
        else map_schema("dbo", target_schema, identifier_case)
    )
    return schema, map_object(seq, identifier_case)
