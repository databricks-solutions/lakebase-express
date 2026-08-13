"""AI fix proposal for a validation inconsistency.

One synchronous Foundation Model call per request (unlike the repair agent's
loop): the user reviews the proposed SQL in an editor and applies it explicitly
through the existing /api/migration/apply endpoint, so the human stays in the
loop for every change made to the target. Fail-soft like the other AI helpers —
returns ``FixProposal(success=False, error=…)`` instead of raising.
"""
from __future__ import annotations

import json
import logging
import re

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

from backend.config import FM_ENDPOINT
from backend.fm_params import chat_text, query_chat
from backend.validation.models import FixProposal, MatchStatus, ValidationItem

log = logging.getLogger("lakebase_express.validation.fixer")

# Prompt-size guards (same scale as the repair agent).
_MAX_SQL_CHARS = 6000

# A translated procedure body can be long; 4000 used to truncate mid-"sql",
# which surfaced as a fix with analysis but an empty (or half-written) SQL string.
_MAX_OUTPUT_TOKENS = 128000

_SYSTEM_PROMPT = """You are a database migration validation remediation agent. A \
post-migration validation compared a source Azure SQL / SQL Server database with its \
migrated Databricks Lakebase (PostgreSQL 15+) target and found an inconsistency. \
Produce PostgreSQL SQL that resolves it in the TARGET database.

Rules:
- Object missing in the target: produce the complete CREATE statement, translating the
  provided T-SQL definition to PostgreSQL (LANGUAGE plpgsql for procedures/functions).
  A T-SQL trigger becomes a trigger function plus a CREATE TRIGGER statement — multiple
  statements are fine; they run in one transaction.
- Structural mismatch: produce ALTER TABLE statements that align the target table with
  the source columns. If a starting-point fix is provided, refine it rather than
  restarting from scratch.
- A row-count mismatch cannot be fixed by SQL alone: return an empty "sql" and explain
  that the table's data should be re-copied.
- Keep the mapped schema and object name exactly as given. Always double-quote mapped
  schema/object identifiers so mixed-case names remain exact and case-sensitive. The
  statements run alone in one transaction and must be self-contained.
- "sql" must contain ONLY executable PostgreSQL statements — never prose, markdown
  fences, or JSON. Leave it empty ONLY when SQL cannot fix the issue (row-count
  mismatch); for a missing object a complete statement is always required.
- The SQL is shown to a human for review before it runs: format it for reading —
  multi-line, one clause per line, indented bodies — never a single long line. Inside
  the JSON string, encode line breaks as \\n.

Respond with ONLY a JSON object with these keys, in this order:
  "analysis": a short diagnosis (2-4 sentences) of the inconsistency and what the SQL does;
  "sql": the complete Postgres SQL, or "" if SQL cannot fix it.
Think through "analysis" first, then produce "sql"."""


def _clip(text: str, limit: int = _MAX_SQL_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def _build_user_prompt(item: ValidationItem, target_schema: str) -> str:
    parts = [
        f'Inconsistency: the {item.kind.value} "{item.target_name}" is {item.status.value}.',
        f'Source object: "{item.source_name or "(none — target-only object)"}". '
        f'The source default schema dbo maps to "{target_schema}".',
    ]
    if item.detail:
        parts.append(f"Detail: {item.detail}")
    if item.source_rows is not None and item.target_rows is not None:
        parts.append(f"Row counts — source: {item.source_rows}, target: {item.target_rows}.")
    if item.columns_missing:
        parts.append(f"Columns missing in the target: {', '.join(item.columns_missing)}.")
    if item.columns_extra:
        parts.append(f"Extra columns in the target: {', '.join(item.columns_extra)}.")
    if item.type_drift:
        parts.append(f"Column type drift: {'; '.join(item.type_drift)}.")
    if item.collation_drift:
        # Spelled out because the model otherwise reaches for ALTER COLUMN SET,
        # which cannot change a collation.
        parts.append(
            f"Column collation drift: {'; '.join(item.collation_drift)}. "
            "A column's collation is changed with ALTER TABLE ... ALTER COLUMN <col> "
            "TYPE <same type> COLLATE <collation>. If the collation itself does not "
            "exist yet, create it first with CREATE COLLATION IF NOT EXISTS "
            "(provider = icu, locale = '<icu locale>', deterministic = <bool>); a "
            "case- or accent-insensitive collation must be nondeterministic."
        )
    if item.objects:
        # Constraint/index/FK rollup: the item covers every object of one kind on
        # one table, so the model needs the per-object breakdown to know which
        # ones to create — the summary count alone is not actionable.
        breakdown = "\n".join(
            f"  - {o.name}: {o.status.value}"
            + (f" ({o.detail})" if o.detail else "")
            + (f" [source definition: {o.source_definition}]" if o.source_definition else "")
            for o in item.objects
            if o.status is not MatchStatus.MATCHED
        )
        if breakdown:
            parts.append(
                f"This item covers all {item.kind.value.replace('_', ' ')} objects on the "
                f"table ({item.objects_present} of {item.objects_expected} present). "
                f"Objects needing attention:\n{breakdown}\n"
                "Create only what is missing; do not drop target-only objects."
            )
    if item.source_definition:
        parts.append(f"Original source T-SQL definition:\n{_clip(item.source_definition)}")
    if item.fix_sql:
        parts.append(f"Deterministic starting-point fix (refine as needed):\n{_clip(item.fix_sql)}")
    parts.append("Produce the remediation SQL.")
    return "\n\n".join(parts)


# Structured output schema: the endpoint is constrained to emit exactly
# {"analysis", "sql"}. Without it, models pretty-print the JSON with raw
# newlines inside the "sql" string — invalid JSON — or run past the token
# limit, and the whole half-JSON blob used to land in the user's editor.
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "validation_fix",
        "schema": {
            "type": "object",
            "properties": {
                "analysis": {
                    "type": "string",
                    "description": "Short diagnosis (2-4 sentences) of the inconsistency and what the SQL does.",
                },
                "sql": {
                    "type": "string",
                    "description": "Executable PostgreSQL statements only — no prose or "
                                   "markdown. Empty only when SQL cannot fix the issue.",
                },
            },
            "required": ["analysis", "sql"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def _parse_proposal(content: str) -> dict[str, str] | None:
    """Extract {"analysis", "sql"} — raw JSON, ```json fences, or JSON in prose.

    ``strict=False`` tolerates raw newlines inside the strings (a common model
    slip). A reply that *looks* like JSON but still won't parse is garbage
    (usually truncated) — return None so the caller reports an error instead of
    dumping the blob into the user's editor. Only a reply with no JSON shape at
    all is treated as bare SQL.
    """
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate, strict=False)
        return {"analysis": str(data.get("analysis") or ""), "sql": str(data.get("sql") or "")}
    except (json.JSONDecodeError, AttributeError):
        if text.lstrip().startswith(("{", "```")):
            return None
        return {"analysis": "Model did not return JSON; reply shown verbatim.", "sql": content}


# --- SQL cleanup ---------------------------------------------------------------------
#
# The proposed SQL lands in an editor for human review, so presentation matters.
# Models routinely hand back SQL wrapped in markdown fences, with double-escaped
# newlines that survive the JSON parse as literal "\n" text, or as one long line.

_SQL_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*\n?(.*?)\n?\s*```$", re.DOTALL)

# Quoted regions the reflow must never touch: 'strings' (with '' escapes),
# "identifiers", and $tag$ dollar-quoted bodies.
_QUOTED_RE = re.compile(r"('(?:[^']|'')*'|\"[^\"]*\")", re.DOTALL)
_DOLLAR_RE = re.compile(r"(\$[A-Za-z_]*\$)(.*?)(\1)", re.DOTALL)

# Clause keywords that start a new line when the statement arrives unformatted.
_CLAUSE_RE = re.compile(
    r"[ \t]+(?=(?:SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|"
    r"(?:LEFT|RIGHT|INNER|FULL|CROSS)\s+JOIN|JOIN|UNION|VALUES|RETURNING|"
    r"INSERT\s+INTO|SET|BEGIN|DECLARE|LANGUAGE|END\b)\b)",
    re.IGNORECASE,
)


def _break_clauses(segment: str) -> str:
    segment = re.sub(r";[ \t]*(?=\S)", ";\n\n", segment)  # one statement per block
    return _CLAUSE_RE.sub("\n", segment)


def _reflow_sql(sql: str) -> str:
    """Give an unformatted (single-line) statement line breaks at clause boundaries,
    leaving quoted strings, identifiers, and already-formatted SQL alone."""
    if len(sql) <= 80 or sql.count("\n") >= 2:
        return sql

    def reflow_plain(text: str) -> str:
        parts = _QUOTED_RE.split(text)  # odd indices are quoted — untouched
        return "".join(p if i % 2 else _break_clauses(p) for i, p in enumerate(parts))

    out, pos = [], 0
    for m in _DOLLAR_RE.finditer(sql):  # recurse into dollar-quoted code bodies
        out.append(reflow_plain(sql[pos:m.start()]))
        out.append(m.group(1) + reflow_plain(m.group(2)) + m.group(3))
        pos = m.end()
    out.append(reflow_plain(sql[pos:]))
    return "".join(out)


def _clean_sql(sql: str) -> str:
    text = (sql or "").strip()
    fenced = _SQL_FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    # Double-escaped whitespace: json.loads leaves it as literal backslash-n text.
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "    ")
    return _reflow_sql(text)


_RETRY_PROMPT = (
    'Your reply left "sql" empty, but this inconsistency is fixable with SQL. '
    'Reply again with the complete executable PostgreSQL statements in the "sql" '
    "field — SQL only, no prose or markdown."
)


def _ask(endpoint: str, messages: list[ChatMessage]) -> tuple[dict[str, str] | None, str]:
    """One structured-output call. Returns (proposal, "") or (None, user-facing error)."""
    resp = query_chat(
        endpoint,
        messages=messages,
        temperature=0.0,
        max_tokens=_MAX_OUTPUT_TOKENS,
        response_format=_RESPONSE_FORMAT,
    )
    choice = resp.choices[0]
    if (choice.finish_reason or "").lower() == "length":
        return None, ("The model hit its output-token limit before finishing the fix — "
                      "run it again, or pick an endpoint with a larger output window in Settings.")
    # chat_text flattens structured content blocks (reasoning models like
    # fable-5) down to the answer text; plain strings pass through.
    parsed = _parse_proposal(chat_text(resp))
    if parsed is None:
        return None, "The model returned malformed JSON instead of a fix — run Fix with AI again."
    return parsed, ""


def propose_fix(
    item: ValidationItem, target_schema: str = "public", endpoint: str | None = None
) -> FixProposal:
    endpoint = endpoint or FM_ENDPOINT
    try:
        messages = [
            ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
            ChatMessage(role=ChatMessageRole.USER,
                        content=_build_user_prompt(item, target_schema)),
        ]
        parsed, error = _ask(endpoint, messages)
        if parsed is None:
            return FixProposal(endpoint=endpoint, success=False, error=error)
        # A missing object always has a SQL fix (CREATE), so a blank "sql" there
        # is the model stopping early — push back once with the reply in context
        # before giving up. Mismatches may legitimately have no SQL (row-count
        # drift is fixed by re-copying data, and the analysis says so). Extra
        # objects never reach the AI: their guarded DROP is deterministic.
        if item.status is MatchStatus.MISSING and not parsed["sql"].strip():
            retry, retry_error = _ask(endpoint, messages + [
                ChatMessage(role=ChatMessageRole.ASSISTANT, content=json.dumps(parsed)),
                ChatMessage(role=ChatMessageRole.USER, content=_RETRY_PROMPT),
            ])
            if retry is None or not retry["sql"].strip():
                return FixProposal(
                    endpoint=endpoint, success=False,
                    error=retry_error or "The model analyzed the inconsistency but returned "
                          "no SQL — run Fix with AI again, or write the fix manually.",
                )
            parsed = retry
        return FixProposal(
            analysis=parsed["analysis"], sql=_clean_sql(parsed["sql"]), endpoint=endpoint,
            success=True,
        )
    except Exception as exc:
        log.warning("Fix proposal failed for %s: %s", item.id, exc)
        return FixProposal(endpoint=endpoint, success=False, error=str(exc))
