"""Mechanical T-SQL scalar-expression -> Postgres translation.

Used for the small expressions that ride on tables — column DEFAULTs, CHECK
constraint predicates, and filtered-index WHERE clauses. These are tiny and
overwhelmingly formulaic (literals, getdate(), newid(), simple comparisons), so
they are translated deterministically — no Foundation Model round trip. The
translation is intentionally conservative: known constructs are rewritten,
everything else is left verbatim so an unsupported expression fails visibly at
apply time (each plan item applies in its own transaction) and can be edited in
the plan UI instead of silently changing meaning.

That "left verbatim" rule extends to conversions whose target type has no faithful
Postgres equivalent: ``CONVERT``/``CAST`` are rewritten only when the type maps
cleanly (see :func:`~backend.schema_migration.type_mapper.map_cast_type`), because a
cast target is chosen deliberately and guessing would change what the expression
computes. Note that a passed-through call is still *syntactically* valid Postgres,
so it fails at execution rather than at parse time.
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

# T-SQL "AT TIME ZONE 'name'": SQL Server names zones in the Windows registry
# form ("E. South America Standard Time"); Postgres only knows IANA names
# ("America/Sao_Paulo") and rejects the Windows name at execution. The clause
# spelling is identical in both engines, so only the quoted name is rewritten.
_AT_TIME_ZONE = re.compile(r"(AT\s+TIME\s+ZONE\s+)'([^']*)'", re.IGNORECASE)


def _ci_windows_tz() -> dict[str, str]:
    """Lower-cased index of the Windows->IANA map, so a name that differs only in
    case still resolves (SQL Server zone names are case-insensitive)."""
    from backend.schema_migration.windows_timezones import WINDOWS_TO_IANA

    return {k.lower(): v for k, v in WINDOWS_TO_IANA.items()}


_CI_WINDOWS_TZ: dict[str, str] = _ci_windows_tz()

# A CONVERT()/CAST() target type: optionally bracket-quoted, with optional
# parenthesised length/precision, e.g. [datetime2](7), varchar(max), [int].
_TYPE_REF = re.compile(
    r"^\[?([A-Za-z_][A-Za-z0-9_]*)\]?(?:\s*\(\s*([^)]*?)\s*\))?$", re.IGNORECASE
)

# Start of a conversion call; the argument span is then matched by paren balance
# (arguments nest and can contain quoted strings, so a regex can't bound them).
_CONVERSION = re.compile(r"\b(CONVERT|CAST)\s*\(", re.IGNORECASE)

# A top-level "AS" separating CAST's value from its type.
_AS_KEYWORD = re.compile(r"\s+AS\s+", re.IGNORECASE)


def _skip_string(text: str, i: int) -> int:
    """Index just past the single-quoted literal starting at ``i`` ('' escapes)."""
    i += 1
    while i < len(text):
        if text[i] == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return i  # unterminated: treat the remainder as the literal


def _matching_paren(text: str, open_paren: int) -> int | None:
    """Index of the ``)`` closing the ``(`` at ``open_paren``, or None if the
    expression is unbalanced (in which case the caller leaves it verbatim)."""
    depth = 0
    i = open_paren
    while i < len(text):
        ch = text[i]
        if ch == "'":
            i = _skip_string(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split ``text`` on ``sep`` at paren depth 0, ignoring quoted strings."""
    parts: list[str] = []
    depth = start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            i = _skip_string(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _split_cast_as(text: str) -> tuple[str, str] | None:
    """Split CAST's inner text into (value, type) on its top-level ``AS``."""
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            i = _skip_string(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            m = _AS_KEYWORD.match(text, i)
            if m:
                return text[:i], text[m.end():]
        i += 1
    return None


def _find_conversion(expr: str, pos: int) -> re.Match[str] | None:
    """Next CONVERT(/CAST( at or after ``pos``, skipping quoted string literals so
    a conversion spelled inside a string value is never rewritten."""
    i = pos
    while i < len(expr):
        if expr[i] == "'":
            i = _skip_string(expr, i)
            continue
        m = _CONVERSION.match(expr, i)
        if m:
            return m
        i += 1
    return None


def _rewrite_conversions(expr: str) -> str:
    """Rewrite ``CONVERT(type, val)`` / ``CAST(val AS type)`` to Postgres
    ``CAST(val AS type)``, recursing into the value expression.

    Must run before the bracket-identifier pass: ``CONVERT([datetime2](7), ...)``
    would otherwise become ``CONVERT("datetime2"(7), ...)``, which Postgres reads
    as a call to a nonexistent ``datetime2(integer)`` function.

    A conversion whose target type can't be translated faithfully — or a
    three-argument ``CONVERT`` with a style code, whose formatting has no direct
    Postgres equivalent — is left verbatim, per this module's fail-visibly policy.
    """
    out: list[str] = []
    pos = 0
    while True:
        m = _find_conversion(expr, pos)
        if not m:
            out.append(expr[pos:])
            return "".join(out)

        close = _matching_paren(expr, m.end() - 1)
        if close is None:
            out.append(expr[pos:])
            return "".join(out)

        inner = expr[m.end():close]
        rewritten: str | None = None

        if m.group(1).lower() == "convert":
            args = _split_top_level(inner)
            # A style code (3rd argument) drives T-SQL-specific formatting.
            if len(args) == 2:
                rewritten = _as_cast(args[0], args[1])
        else:
            split = _split_cast_as(inner)
            if split:
                rewritten = _as_cast(split[1], split[0])

        if rewritten is None:
            # Leave the call as written, but still translate what's inside it.
            out.append(expr[pos:m.end()])
            out.append(_rewrite_conversions(inner))
            out.append(")")
        else:
            out.append(expr[pos:m.start()])
            out.append(rewritten)
        pos = close + 1


def _as_cast(type_text: str, value: str) -> str | None:
    """``CAST(value AS pg_type)`` for a translatable target type, else None."""
    from backend.schema_migration.type_mapper import map_cast_type

    m = _TYPE_REF.match(type_text.strip())
    if not m:
        return None
    params = _split_top_level(m.group(2)) if m.group(2) else None
    pg_type = map_cast_type(m.group(1), params)
    if pg_type is None:
        return None
    return f"CAST({_rewrite_conversions(value.strip())} AS {pg_type})"


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

# ``"Col" = (1)`` / ``"Col" <> 0`` against a bit column, as T-SQL writes it in a
# filtered-index or CHECK predicate. The column becomes Postgres boolean, where
# comparing to an integer is an error ("operator does not exist: boolean =
# integer") rather than a silent coercion — so the literal is rewritten to
# true/false. Matched after the bracket pass, hence the double-quoted name.
#
# The literal's own parentheses are matched as a balanced pair rather than with a
# greedy ``\)*``: T-SQL wraps the whole predicate too (``([Active]=(1))``), and a
# greedy match would swallow that outer ``)`` and unbalance the expression.
_BIT_COMPARISON = re.compile(
    r'"(?P<col>[^"]+)"\s*(?P<op>=|<>|!=)\s*'
    r'(?P<open>\(*)\s*(?P<value>[01])\s*(?P<close>\)*)'
)

# ``"Col" IS NULL`` and arithmetic on a bit column are left alone: IS NULL is
# valid for boolean, and anything beyond a straight 0/1 comparison is unusual
# enough that guessing would risk changing meaning (this module's fail-visibly
# policy). Only the two-value equality form above is rewritten.


def _bit_columns(columns) -> frozenset[str]:
    """Names of ``bit`` columns, which become Postgres boolean."""
    return frozenset(
        c.name for c in (columns or []) if (c.data_type or "").lower() == "bit"
    )


def _rewrite_bit_comparisons(expr: str, bit_columns: frozenset[str]) -> str:
    """Rewrite ``bit`` comparisons against 0/1 to boolean true/false."""
    if not bit_columns:
        return expr

    def _repl(m: re.Match[str]) -> str:
        if m.group("col") not in bit_columns:
            return m.group(0)
        literal = "true" if m.group("value") == "1" else "false"
        op = "<>" if m.group("op") in {"<>", "!="} else "="
        # Keep only the parens that wrap the literal itself; any extra closer
        # belongs to an enclosing group and must stay where it was.
        depth = min(len(m.group("open")), len(m.group("close")))
        kept = m.group("close")[depth:]
        return f'"{m.group("col")}" {op} {"(" * depth}{literal}{")" * depth}{kept}'

    return _BIT_COMPARISON.sub(_repl, expr)


def _translate_time_zones(expr: str) -> str:
    """Rewrite the zone name in any ``AT TIME ZONE 'name'`` clause from its
    Windows (SQL Server) form to the IANA name Postgres requires.

    An unrecognised name is left verbatim (fail-visibly) — except one Postgres
    already accepts (e.g. 'UTC'), which is left as-is because it needs no change.
    """
    def _repl(m: re.Match[str]) -> str:
        iana = _CI_WINDOWS_TZ.get(m.group(2).lower())
        return f"{m.group(1)}'{iana}'" if iana else m.group(0)

    return _AT_TIME_ZONE.sub(_repl, expr)


def map_expression(expr: str, *, columns=None) -> str:
    """Translate a T-SQL scalar expression to its Postgres form.

    Handles bracket quoting ([Col] -> "Col", case preserved to match the
    scanned column names the tables keep), N'..' literals, the common
    date/uuid/null functions, and Windows time-zone names in AT TIME ZONE
    clauses. Unknown constructs pass through verbatim.

    ``columns`` is the scanned column list of the table the expression belongs
    to. It is what makes a ``bit`` column's ``= 1`` comparison translatable:
    ``bit`` maps to Postgres boolean, and ``boolean = integer`` is an error
    there, so the predicate needs the column's type to be fixed up. Without it
    the expression is still translated, just not that part.
    """
    out = (expr or "").strip()

    # Before the bracket pass, which would turn a cast's target type into a
    # quoted identifier (and thus a call to a function that doesn't exist).
    out = _rewrite_conversions(out)
    out = _translate_time_zones(out)

    # Protect nothing fancy: T-SQL system expressions here don't nest strings
    # with brackets in practice, so plain regex passes are sufficient.
    out = _BRACKET_IDENT.sub(r'"\1"', out)
    out = _NSTRING.sub(r"\1", out)

    # After the bracket pass, so the column name is already double-quoted.
    out = _rewrite_bit_comparisons(out, _bit_columns(columns))

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
