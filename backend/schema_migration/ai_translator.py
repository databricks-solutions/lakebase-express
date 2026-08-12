"""T-SQL -> PL/pgSQL translation via a Databricks Foundation Model.

Calls a Model Serving / AI Gateway chat endpoint (default from config.FM_ENDPOINT)
through the native databricks-sdk wire API — no ``openai`` package required.
Structured output (``response_format`` json_schema) constrains the reply to
{"reasoning", "translated", "notes"}; the parse is still defensive (code fences,
surrounding prose) for endpoints that don't honor it, and never lets a malformed
JSON blob through as SQL.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

from backend.assessment.models import ProgrammableObject
from backend.config import FM_ENDPOINT
from backend.fm_params import chat_text, query_chat
from backend.schema_migration.models import Translation
from backend.schema_migration.naming import (
    IdentifierCase,
    map_object,
    trigger_function_name,
)
from backend.schema_migration.trigger_sql import sanitize_trigger_sql

log = logging.getLogger("lakebase_express.ai_translator")

_SYSTEM_PROMPT = """You are a senior database migration engineer. You convert \
Microsoft T-SQL (Azure SQL) into PostgreSQL 15+ compatible SQL / PL/pgSQL for \
Databricks Lakebase.

Rules:
- Stored procedures -> CREATE OR REPLACE PROCEDURE ... LANGUAGE plpgsql.
- Scalar/table functions -> CREATE OR REPLACE FUNCTION.
- Views -> CREATE OR REPLACE VIEW.
- Convert: ISNULL->COALESCE, GETDATE()->now(), TOP n->LIMIT n, [id]->"id",
  '+' string concat-> ||, @@IDENTITY/SCOPE_IDENTITY-> RETURNING, TRY/CATCH->
  BEGIN...EXCEPTION, #temp-> TEMP TABLE, INSERTED/DELETED-> NEW/OLD.
- COLLATE: a SQL Server collation name is not a Postgres one. The migration creates
  each source collation in the target under its own lower-cased name (e.g.
  COLLATE SQL_Latin1_General_CP1_CI_AS -> COLLATE "sql_latin1_general_cp1_ci_as"),
  so keep the clause and just requote the name that way. A binary collation
  (_BIN/_BIN2) becomes COLLATE "C". Note that case-insensitive collations are
  nondeterministic in Postgres, so LIKE/regex against such a column is rejected —
  if the source code pattern-matches one, use lower(col) LIKE lower(...) instead
  and say so in notes.
- If a construct has no faithful equivalent, keep best-effort code and explain in notes.
- "translated" must contain ONLY executable PostgreSQL / PL-pgSQL — never prose,
  markdown fences, or JSON.

Respond with ONLY a JSON object with these keys, in this order:
  "reasoning": a short step-by-step explanation (2-5 sentences) of how you analyzed
               the source and the key T-SQL -> Postgres decisions you made;
  "translated": the Postgres SQL;
  "notes": brief migration caveats the reviewer must check.
Think through "reasoning" first, then produce "translated"."""

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
        f"{guidance}Translate this T-SQL {obj.object_type.lower()} named "
        f'"{obj.schema_name}.{obj.object_name}":\n\n{obj.definition}'
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


def translate_object(
    obj: ProgrammableObject,
    endpoint: str | None = None,
    schema_map: dict[str, str] | None = None,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> Translation:
    endpoint = endpoint or FM_ENDPOINT
    try:
        resp = query_chat(
            endpoint,
            messages=[
                ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                ChatMessage(
                    role=ChatMessageRole.USER,
                    content=_build_user_prompt(obj, schema_map, identifier_case),
                ),
            ],
            temperature=0.0,
            max_tokens=_MAX_OUTPUT_TOKENS,
            response_format=_RESPONSE_FORMAT,
        )
        choice = resp.choices[0]
        name = f"{obj.schema_name}.{obj.object_name}"
        if (choice.finish_reason or "").lower() == "length":
            return Translation(
                object_name=name, object_type=obj.object_type, original=obj.definition,
                translated="", reasoning="",
                notes="The model hit its output-token limit before finishing — re-run the "
                      "translation, or pick an endpoint with a larger output window in Settings.",
                success=False,
            )
        # chat_text flattens structured content blocks (reasoning models like
        # fable-5) down to the answer text; plain strings pass through.
        payload = _parse_payload(chat_text(resp))
        if payload is None:
            return Translation(
                object_name=name, object_type=obj.object_type, original=obj.definition,
                translated="", reasoning="",
                notes="The model returned malformed JSON instead of a translation — "
                      "re-run the translation.",
                success=False,
            )
        translated = payload["translated"]
        # Deterministic guardrail at translation time; also re-applied when the
        # SQL is actually applied (executor / post-load notebook), so a stale or
        # hand-edited plan is corrected regardless of when it was built.
        if obj.object_type.upper() == "TRIGGER":
            translated = sanitize_trigger_sql(translated)
        return Translation(
            object_name=name,
            object_type=obj.object_type,
            original=obj.definition,
            translated=translated,
            reasoning=payload["reasoning"],
            notes=payload["notes"],
            success=True,
        )
    except Exception as exc:  # one bad object shouldn't fail the batch
        log.exception("Translation failed for %s.%s", obj.schema_name, obj.object_name)
        return Translation(
            object_name=f"{obj.schema_name}.{obj.object_name}",
            object_type=obj.object_type,
            original=obj.definition,
            translated="",
            reasoning="",
            notes=f"Translation error: {exc}",
            success=False,
        )


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
