"""T-SQL -> PL/pgSQL translation via a Databricks Foundation Model.

Calls a Model Serving / AI Gateway chat endpoint (default from config.FM_ENDPOINT)
through the native databricks-sdk wire API — no ``openai`` package required.
Structured output (``response_format`` json_schema) constrains the reply to
{"reasoning", "translated", "notes"}; the parse is still defensive (code fences,
surrounding prose) for endpoints that don't honor it, and never lets a malformed
JSON blob through as SQL.

The system prompt is a Jinja template in ``./prompts`` (see backend/prompts.py) so
the translation rules can be tuned as prose. The user message stays in Python: it
is per-object logic — schema mapping, trigger naming, and the scratch-collection
decisions from assessment/temp_objects.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

from backend.assessment import callable_shape
from backend.assessment.models import ProgrammableObject
from backend.assessment.temp_objects import (
    REWRITES,
    Strategy,
    analyze,
    temp_table_regression,
)
from backend.config import FM_ENDPOINT
from backend.fm_params import chat_text, query_chat
from backend.prompts import render
from backend.schema_migration.models import Translation
from backend.schema_migration.naming import (
    IdentifierCase,
    map_object,
    trigger_function_name,
)
from backend.schema_migration.trigger_sql import sanitize_trigger_sql

log = logging.getLogger("lakebase_express.ai_translator")


def _system_prompt(sql_only: bool = False) -> str:
    return render(__file__, "tsql_translation.system.jinja", sql_only=sql_only)


# Structured output schema: the endpoint is constrained to emit exactly
# {"reasoning", "translated", "notes"}. Without it, models pretty-print the JSON
# with raw newlines inside "translated" — invalid JSON — and the whole blob used
# to land verbatim in the plan's SQL editor. Endpoints that reject the parameter
# degrade gracefully to free-form output (see fm_params.query_chat).
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "tsql_translation",
        "schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Short step-by-step explanation (2-5 sentences) of the "
                                   "key T-SQL -> Postgres decisions.",
                },
                "translated": {
                    "type": "string",
                    "description": "Executable PostgreSQL / PL-pgSQL only — no prose or markdown.",
                },
                "notes": {
                    "type": "string",
                    "description": "Brief migration caveats the reviewer must check.",
                },
            },
            "required": ["reasoning", "translated", "notes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

# A long procedure body can exceed 4000 completion tokens, truncating mid-"translated".
_MAX_OUTPUT_TOKENS = 128000

# Bounds the SQL-only fallback for replies past the endpoint's output window.
_MAX_CONTINUATIONS = 4
# A repeated tail shorter than this is likelier coincidence than the model restarting.
_MIN_OVERLAP = 16
_OVERLAP_SCAN = 4000

_CONTINUE = (
    "Your reply was cut off by the output limit. Continue from exactly where it "
    "stopped — do not repeat anything already written, and add no preamble or fences."
)
_FIX = (
    "That translation has problems: {problems}. Return the complete corrected "
    "translation in the same format."
)
_SQL_ONLY_NOTE = (
    "Too long for one structured reply, so it was translated as bare SQL across "
    "several passes; no reasoning was captured and caveats are in -- comments. "
    "Review the joins between passes."
)
_TOKEN_LIMIT_NOTE = (
    "The model hit its output-token limit before finishing, even continued across "
    "several passes — pick an endpoint with a larger output window in Settings, or "
    "split the object."
)
_MALFORMED_NOTE = (
    "The model returned malformed JSON instead of a translation — re-run the translation."
)

# Leftovers that cannot be valid Postgres, so the translation is incomplete.
_TSQL_RESIDUE = (
    (re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE), "a T-SQL GO batch separator"),
    (re.compile(r"\bDECLARE\s+@", re.IGNORECASE), "a T-SQL @variable declaration"),
    (re.compile(r"\[\w+\]\.\[\w+\]"), "a bracket-quoted [schema].[name]"),
)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_DOLLAR_QUOTE = re.compile(r"\$([A-Za-z_]\w*)?\$")
_FENCE_OPEN = re.compile(r"\A\s*```[A-Za-z]*[ \t]*\n?")
_FENCE_CLOSE = re.compile(r"\n?```\s*\Z")


def _temp_guidance(definition: str) -> str:
    """Per-collection instructions for the scratch tables in this body.

    The same analysis the assessment ran, so the model is told to do exactly what
    the user was told would happen. Row counts are not passed: sizing escalates a
    finding for a human, and a model handed a row count would quietly decide the
    rewrite instead of flagging it.
    """
    usages = analyze(definition)
    if not usages:
        return ""
    lines = [f"  - {u.summary}" for u in usages]
    wanted = {u.strategy for u in usages}
    how = "\n".join(f"  {REWRITES[s]}" for s in Strategy if s in wanted)
    return (
        "This body uses scratch collections. Rewrite each one as shown — none of them "
        "may become a Postgres TEMP TABLE:\n"
        + "\n".join(lines)
        + f"\n\nHow to apply those:\n{how}\n\n"
    )


def _build_user_prompt(
    obj: ProgrammableObject,
    schema_map: dict[str, str] | None = None,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    guidance = ""
    if schema_map:
        dst = schema_map.get(obj.schema_name, "public")
        name = map_object(obj.object_name, identifier_case)
        pairs = ", ".join(f"{src} -> {d}" for src, d in sorted(schema_map.items()))
        naming_rule = (
            "Preserve the exact source casing of every schema and object identifier and "
            "double-quote every identifier"
            if identifier_case == IdentifierCase.PRESERVE
            else "Lower-case all schema and object identifiers"
        )
        quoted = identifier_case == IdentifierCase.PRESERVE
        fn = trigger_function_name(obj.object_name, identifier_case)
        qdst = f'"{dst}"' if quoted else dst
        qname = f'"{name}"' if quoted else name
        qfn = f'"{fn}"' if quoted else fn
        qtable = '"<table>"' if quoted else "<table>"
        if obj.object_type.upper() == "TRIGGER":
            # Postgres trigger names are NEVER schema-qualified (a trigger lives
            # on its table) — CREATE TRIGGER schema.name is a syntax error. The
            # schema goes on the ON clause and the trigger function instead.
            guidance = (
                f"Create this trigger named {qname} — do NOT schema-qualify the trigger name "
                f"(Postgres rejects it); schema-qualify the table in the ON clause and the "
                f"trigger function instead, e.g.\n"
                f"  CREATE OR REPLACE FUNCTION {qdst}.{qfn}() RETURNS trigger ...;\n"
                f"  CREATE OR REPLACE TRIGGER {qname} ... ON {qdst}.{qtable} "
                f"FOR EACH ROW EXECUTE FUNCTION {qdst}.{qfn}();\n"
                f"Qualify EVERY referenced table/object with its mapped schema. {naming_rule}, "
                f"using this schema mapping: {pairs}.\n\n"
            )
        else:
            guidance = (
                f'Create this object as "{dst}"."{name}" '
                f'(e.g. CREATE OR REPLACE {obj.object_type.lower()} "{dst}"."{name}" ...).\n'
                f"Qualify EVERY referenced table/object with its mapped schema. {naming_rule}, "
                f"using this schema mapping: {pairs}.\n\n"
            )
    return (
        f"{guidance}{_temp_guidance(obj.definition)}Translate this T-SQL "
        f'{obj.object_type.lower()} named "{obj.schema_name}.{obj.object_name}":'
        f"\n\n{obj.definition}"
    )


def _parse_payload(content: str) -> dict[str, str] | None:
    """Extract {"reasoning", "translated", "notes"} from the model's reply.

    Handles raw JSON, ```json fenced blocks, and JSON embedded in prose;
    ``strict=False`` tolerates raw newlines inside the strings (a common model
    slip). A reply that *looks* like JSON but still won't parse is garbage
    (usually truncated) — return None so the caller marks the object failed
    instead of dumping the blob into the plan's SQL editor. Only a reply with
    no JSON shape at all is treated as bare SQL.
    """
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    # Otherwise grab the outermost {...} span.
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate, strict=False)
        return {
            "reasoning": str(data.get("reasoning") or ""),
            "translated": str(data.get("translated") or ""),
            "notes": str(data.get("notes") or ""),
        }
    except (json.JSONDecodeError, AttributeError):
        if text.lstrip().startswith(("{", "```")):
            return None
        return {
            "reasoning": "",
            "translated": content,
            "notes": "Model did not return JSON; output shown verbatim — review.",
        }


def _unfence(sql: str) -> str:
    """Drop a markdown fence the model wrapped the SQL in, despite the prompt."""
    return _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", sql)).strip()


def _stitch(head: str, tail: str) -> str:
    """Join a continuation onto truncated text, dropping a tail the model repeated."""
    for k in range(min(len(head), len(tail), _OVERLAP_SCAN), _MIN_OVERLAP - 1, -1):
        if head.endswith(tail[:k]):
            return head + tail[k:]
    return head + tail


def _problems(payload: dict[str, str] | None) -> list[str]:
    """Deterministic checks that the translation is complete Postgres, not T-SQL."""
    if payload is None:
        return ["the reply was not the requested JSON object"]
    sql = payload["translated"]
    if not sql.strip():
        return ['"translated" is empty']
    # Caveat comments often name the T-SQL they replaced.
    sql = _COMMENT.sub("", sql)
    found = []
    if not re.search(r"\bCREATE\b", sql, re.IGNORECASE):
        found.append("it has no CREATE statement")
    tags: dict[str, int] = {}
    for m in _DOLLAR_QUOTE.finditer(sql):
        tags[m.group(0)] = tags.get(m.group(0), 0) + 1
    found += [f"the {tag} body is never closed" for tag, n in tags.items() if n % 2]
    found += [f"it still contains {what}" for rx, what in _TSQL_RESIDUE if rx.search(sql)]
    return found


def _ask(endpoint: str, messages: list[ChatMessage], structured: bool) -> tuple[str, bool]:
    """One chat call -> (reply text, whether it hit the output-token limit)."""
    resp = query_chat(
        endpoint,
        messages=messages,
        temperature=0.0,
        max_tokens=_MAX_OUTPUT_TOKENS,
        **({"response_format": _RESPONSE_FORMAT} if structured else {}),
    )
    # chat_text flattens structured content blocks (reasoning models like
    # fable-5) down to the answer text; plain strings pass through.
    return chat_text(resp), (resp.choices[0].finish_reason or "").lower() == "length"


def _ask_sql(endpoint: str, messages: list[ChatMessage]) -> str | None:
    """Bare-SQL reply, continued while it truncates; None if still unfinished.

    Bare SQL is used because it concatenates cleanly across passes; a JSON string
    cut mid-escape does not.
    """
    text, truncated = _ask(endpoint, messages, structured=False)
    # Only the opening fence per pass: whitespace at a cut can be load-bearing.
    sql = _FENCE_OPEN.sub("", text)
    for _ in range(_MAX_CONTINUATIONS):
        if not truncated:
            break
        more, truncated = _ask(endpoint, [
            *messages,
            ChatMessage(role=ChatMessageRole.ASSISTANT, content=sql),
            ChatMessage(role=ChatMessageRole.USER, content=_CONTINUE),
        ], structured=False)
        sql = _stitch(sql, _FENCE_OPEN.sub("", more))
    return None if truncated else _unfence(sql)


def _to_payload(text: str, sql_only: bool) -> dict[str, str] | None:
    if sql_only:
        return {"reasoning": "", "translated": text, "notes": _SQL_ONLY_NOTE}
    payload = _parse_payload(text)
    if payload is not None:
        payload["translated"] = _unfence(payload["translated"])
    return payload


def _failed(obj: ProgrammableObject, notes: str) -> Translation:
    return Translation(
        object_name=f"{obj.schema_name}.{obj.object_name}",
        object_type=obj.object_type,
        original=obj.definition,
        translated="",
        reasoning="",
        notes=notes,
        success=False,
    )


def translate_object(
    obj: ProgrammableObject,
    endpoint: str | None = None,
    schema_map: dict[str, str] | None = None,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> Translation:
    """Translate one object: structured reply, a bare-SQL fallback when that is too
    long, and one corrective retry if the result fails :func:`_problems`."""
    endpoint = endpoint or FM_ENDPOINT
    try:
        user = ChatMessage(
            role=ChatMessageRole.USER,
            content=_build_user_prompt(obj, schema_map, identifier_case),
        )
        messages = [ChatMessage(role=ChatMessageRole.SYSTEM, content=_system_prompt()), user]
        text, sql_only = _ask(endpoint, messages, structured=True)
        if sql_only:
            log.warning("Translation of %s.%s truncated — retrying as bare SQL",
                        obj.schema_name, obj.object_name)
            messages = [ChatMessage(role=ChatMessageRole.SYSTEM,
                                    content=_system_prompt(sql_only=True)), user]
            text = _ask_sql(endpoint, messages)
            if text is None:
                return _failed(obj, _TOKEN_LIMIT_NOTE)
        payload = _to_payload(text, sql_only)
        problems = _problems(payload)
        if problems:
            # Shown its own output and what is wrong with it; a plain re-run at
            # temperature 0 would likely repeat the same reply.
            log.warning("Translation of %s.%s failed validation (%s) — retrying",
                        obj.schema_name, obj.object_name, "; ".join(problems))
            messages = [
                *messages,
                ChatMessage(role=ChatMessageRole.ASSISTANT, content=text),
                ChatMessage(role=ChatMessageRole.USER,
                            content=_FIX.format(problems="; ".join(problems))),
            ]
            if sql_only:
                text = _ask_sql(endpoint, messages)
            else:
                text, truncated = _ask(endpoint, messages, structured=True)
                text = None if truncated else text
            if text is None:
                return _failed(obj, _TOKEN_LIMIT_NOTE)
            payload = _to_payload(text, sql_only)
            problems = _problems(payload)
        if payload is None:
            return _failed(obj, _MALFORMED_NOTE)
        translated = payload["translated"]
        # Deterministic guardrail at translation time; also re-applied when the
        # SQL is actually applied (executor / post-load notebook), so a stale or
        # hand-edited plan is corrected regardless of when it was built.
        if obj.object_type.upper() == "TRIGGER":
            translated = sanitize_trigger_sql(translated)
        # Not rewritten, only reported: a temp table the prompt forbade is still
        # applicable SQL, but it breaks on the pooled endpoint and the reviewer has
        # to be told rather than left to notice.
        notes = payload["notes"]
        for regression in (
            temp_table_regression(obj.definition, translated),
            # A row-returning procedure left as a Postgres PROCEDURE cannot be called
            # for its rows at all, and the application cannot compensate — so it is
            # reported here rather than discovered by whoever migrates the caller.
            callable_shape.shape_regression(obj.object_type, obj.definition, translated),
        ):
            if regression:
                notes = f"{notes.rstrip()}\n\n{regression}" if notes.strip() else regression
        if problems:
            # Kept so the reviewer can fix it in the plan, but not marked a success.
            failed = "Failed validation after a retry: " + "; ".join(problems) + "."
            notes = f"{failed}\n\n{notes}" if notes.strip() else failed
        return Translation(
            object_name=f"{obj.schema_name}.{obj.object_name}",
            object_type=obj.object_type,
            original=obj.definition,
            translated=translated,
            reasoning=payload["reasoning"],
            notes=notes,
            success=not problems,
        )
    except Exception as exc:  # one bad object shouldn't fail the batch
        log.exception("Translation failed for %s.%s", obj.schema_name, obj.object_name)
        return _failed(obj, f"Translation error: {exc}")


# Objects are translated concurrently — each translate_object is an independent
# FM call. Bounded so a large schema doesn't open dozens of endpoint connections
# at once; the endpoint's own concurrency is the real ceiling anyway.
_TRANSLATE_WORKERS = 8


def translate_all(
    objects: list[ProgrammableObject],
    endpoint: str | None = None,
    schema_map: dict[str, str] | None = None,
    on_done: Callable[[int, int], None] | None = None,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> list[Translation]:
    """Translate every object, concurrently. Order of the returned list matches
    ``objects``. ``on_done(completed, total)`` is called after each finishes, for
    progress reporting; it must be thread-safe (it's invoked from worker threads).
    """
    if not objects:
        return []
    total = len(objects)
    results: list[Translation | None] = [None] * total
    completed = 0
    lock = threading.Lock()

    def work(i: int) -> None:
        nonlocal completed
        results[i] = translate_object(objects[i], endpoint, schema_map, identifier_case)
        if on_done is not None:
            with lock:
                completed += 1
                on_done(completed, total)

    workers = min(_TRANSLATE_WORKERS, total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # list() forces any unexpected exception in a worker to surface here
        # rather than being swallowed (translate_object itself is fail-soft).
        list(pool.map(work, range(total)))
    return [r for r in results if r is not None]
