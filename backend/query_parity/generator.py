"""AI generation of synthetic query pairs for post-migration parity testing.

One Foundation Model call produces ``count`` read-only queries over the migrated
schema, each expressed twice: once in T-SQL (run against the source) and once in
the equivalent PostgreSQL (run against the Lakebase target). The model is given a
token-bounded summary of the scanned tables (names mapped through the migration's
naming rules, plus column names/types) so the SQL references real objects on both
sides.

Fail-soft like the other AI helpers — returns ``success=False`` with an error
instead of raising, so the module degrades to "couldn't generate" rather than a
500. Queries must be strictly read-only; the runner independently guards against
anything that isn't a ``SELECT``/``WITH`` before executing.
"""
from __future__ import annotations

import json
import logging
import re
import uuid

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

from backend.assessment.models import TableInfo
from backend.config import FM_ENDPOINT
from backend.fm_params import chat_text, query_chat
from backend.query_parity.models import GenerateQueriesResponse, SyntheticQuery
from backend.schema_migration.naming import IdentifierCase, map_object, map_schema

log = logging.getLogger("lakebase_express.query_parity.generator")

# Keep the schema context bounded regardless of source size.
_MAX_TABLES = 40
_MAX_COLS = 20

# Reasoning models spend thinking tokens from the same budget; ask big and let
# query_chat clamp to the endpoint's real output window.
_MAX_OUTPUT_TOKENS = 128000

_SYSTEM_PROMPT = """You are a database migration validation engineer. A database was \
migrated from Azure SQL / SQL Server (T-SQL) to a Databricks Lakebase (PostgreSQL 15+) \
target. To confirm the migration preserved query behaviour, you write synthetic, \
READ-ONLY queries that will be run against BOTH databases and their results compared.

For each query, produce the SAME query intent expressed twice:
- "source_sql": T-SQL for the ORIGINAL source (uses the source schema/table names given).
- "target_sql": the equivalent PostgreSQL for the migrated target (uses the MAPPED
  Postgres schema/table names given).

Rules:
- Queries MUST be strictly read-only: a single SELECT statement (a leading WITH/CTE is
  fine). Never write INSERT/UPDATE/DELETE/MERGE/DDL or call procedures.
- Exercise a VARIETY of behaviour across the batch: simple projections, WHERE filters,
  aggregations (COUNT/SUM/AVG/GROUP BY), multi-table JOINs, ORDER BY, and window
  functions where the schema supports them. Prefer queries whose results are
  deterministic so both sides return identical rows — always add an ORDER BY over a
  stable key when returning row detail, and a LIMIT/TOP to keep result sets small.
- Reference only tables and columns from the provided schema. Match column names exactly
  as given (case-sensitive). Quote mixed-case Postgres identifiers with double quotes.
- The two dialects must be SEMANTICALLY EQUIVALENT: T-SQL "SELECT TOP 10 ..." becomes
  PostgreSQL "SELECT ... LIMIT 10"; bracket-quoting [x] becomes double-quoting "x";
  GETDATE() becomes now(); ISNULL(a,b) becomes COALESCE(a,b); string concat + becomes ||.
- "source_sql" and "target_sql" must contain ONLY the executable query — no prose,
  markdown fences, comments, or trailing semicolons required.

Respond with ONLY a JSON object: {"queries": [ ... ]}. Produce exactly the requested
number of queries."""

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "synthetic_queries",
        "schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "intent": {"type": "string"},
                            "category": {
                                "type": "string",
                                "enum": ["read", "filter", "aggregation", "join", "window"],
                            },
                            "source_sql": {"type": "string"},
                            "target_sql": {"type": "string"},
                        },
                        "required": [
                            "title", "intent", "category", "source_sql", "target_sql",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["queries"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def _schema_context(
    tables: list[TableInfo], target_schema: str, identifier_case: IdentifierCase | str
) -> str:
    """Compact, token-bounded schema summary with both source and mapped names."""
    lines: list[str] = [
        "SCHEMA (source name → mapped Postgres name · columns):",
    ]
    for t in tables[:_MAX_TABLES]:
        tgt_schema = map_schema(t.schema_name, target_schema, identifier_case)
        tgt_table = map_object(t.table_name, identifier_case)
        cols = ", ".join(
            f"{c.name} {c.data_type}" for c in t.columns[:_MAX_COLS]
        )
        if len(t.columns) > _MAX_COLS:
            cols += f", …(+{len(t.columns) - _MAX_COLS} more)"
        pk = f" · PK({', '.join(t.primary_key)})" if t.primary_key else ""
        lines.append(
            f"- source [{t.schema_name}].[{t.table_name}] → "
            f'"{tgt_schema}"."{tgt_table}" · {t.row_count:,} rows{pk}\n    columns: {cols}'
        )
    if len(tables) > _MAX_TABLES:
        lines.append(f"  …and {len(tables) - _MAX_TABLES} more tables")
    return "\n".join(lines)


def _extract_queries(content: str) -> list[dict]:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            candidate = candidate[start : end + 1]
    # strict=False tolerates raw newlines inside string values (a common model slip).
    data = json.loads(candidate, strict=False)
    queries = data.get("queries") if isinstance(data, dict) else None
    return [q for q in (queries or []) if isinstance(q, dict)]


def generate_queries(
    tables: list[TableInfo],
    count: int,
    target_schema: str = "public",
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    endpoint: str | None = None,
) -> GenerateQueriesResponse:
    """Ask the Foundation Model for ``count`` synthetic read-only query pairs.

    Never raises — returns ``success=False`` with an error message on any failure.
    """
    endpoint = endpoint or FM_ENDPOINT
    if not tables:
        return GenerateQueriesResponse(
            endpoint=endpoint, success=False,
            error="No tables found in the source scan — run the Assessment first so there "
                  "is a schema to generate queries against.",
        )
    try:
        user_prompt = (
            f"{_schema_context(tables, target_schema, identifier_case)}\n\n"
            f"Generate exactly {count} synthetic read-only query pair"
            f"{'s' if count != 1 else ''} following the rules."
        )
        resp = query_chat(
            endpoint,
            messages=[
                ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                ChatMessage(role=ChatMessageRole.USER, content=user_prompt),
            ],
            temperature=0.4,  # a little variety across the batch
            max_tokens=_MAX_OUTPUT_TOKENS,
            response_format=_RESPONSE_FORMAT,
        )
        if (resp.choices[0].finish_reason or "").lower() == "length":
            return GenerateQueriesResponse(
                endpoint=endpoint, success=False,
                error="The model hit its output-token limit before finishing — ask for fewer "
                      "queries, or pick an endpoint with a larger output window in Settings.",
            )
        raw = _extract_queries(chat_text(resp))
        queries: list[SyntheticQuery] = []
        for i, q in enumerate(raw):
            source_sql = str(q.get("source_sql") or "").strip()
            target_sql = str(q.get("target_sql") or "").strip()
            if not source_sql or not target_sql:
                continue
            queries.append(SyntheticQuery(
                id=f"q{i + 1}-{uuid.uuid4().hex[:6]}",
                title=str(q.get("title") or f"Query {i + 1}").strip(),
                intent=str(q.get("intent") or "").strip(),
                category=str(q.get("category") or "read").strip().lower(),
                source_sql=source_sql,
                target_sql=target_sql,
            ))
        if not queries:
            return GenerateQueriesResponse(
                endpoint=endpoint, success=False,
                error="The model did not return any usable queries — try again, or pick a "
                      "different Foundation Model endpoint in Settings.",
            )
        return GenerateQueriesResponse(queries=queries, endpoint=endpoint, success=True)
    except Exception as exc:  # fail soft
        log.exception("Synthetic query generation failed (endpoint=%s)", endpoint)
        return GenerateQueriesResponse(endpoint=endpoint, success=False, error=str(exc))
