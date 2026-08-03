"""Deterministic fix-ups for AI-translated Postgres trigger DDL.

Applied where trigger SQL is **applied** (the sync executor and the async
post-load notebook), not only where it's translated — the migration plan stores
whatever SQL was produced, so a plan built before this fix (or hand-edited) must
still be corrected at apply time. Two problems the model reliably introduces,
even when the prompt forbids them (observed on Opus 4.8):

  1. Schema-qualifying the trigger *name* — ``CREATE TRIGGER schema.name`` —
     which Postgres rejects with ``syntax error at or near "."`` (a trigger is
     named by the table it lives on). The trigger *function* IS schema-qualified
     and must stay that way, so we only strip the qualifier from the name.
  2. Emitting plain ``CREATE TRIGGER`` (no ``OR REPLACE``), so a trigger left by
     a prior partial run fails the re-run with "already exists" — upgraded to
     ``CREATE OR REPLACE TRIGGER`` (Postgres 14+) for idempotent re-runs.

Kept dependency-free so both the FastAPI executor and the generated notebook
text can rely on it without pulling in the AI/serving stack.
"""
from __future__ import annotations

import re

# Plain ``CREATE TRIGGER`` (not already OR REPLACE, not CONSTRAINT) -> add
# OR REPLACE. The negative lookahead keeps it from touching a statement that is
# already ``CREATE OR REPLACE TRIGGER``.
_PLAIN_CREATE_TRIGGER = re.compile(
    r"\bCREATE\s+(?!OR\s+REPLACE\b)(?!CONSTRAINT\b)TRIGGER\b", re.IGNORECASE
)

# The identifier right after CREATE [OR REPLACE] [CONSTRAINT] TRIGGER, when it's
# schema-qualified. Group 1 = the CREATE … TRIGGER lead (kept); the schema
# qualifier + dot is dropped; group 2 = the bare trigger name (kept).
_QUALIFIED_TRIGGER_NAME = re.compile(
    r"(CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+)"
    r"(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_$]*)\s*\.\s*"
    r"(\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_$]*)",
    re.IGNORECASE,
)


def sanitize_trigger_sql(sql: str) -> str:
    """Return trigger DDL that applies cleanly and idempotently on Postgres."""
    sql = _PLAIN_CREATE_TRIGGER.sub("CREATE OR REPLACE TRIGGER", sql)
    return _QUALIFIED_TRIGGER_NAME.sub(r"\1\2", sql)
