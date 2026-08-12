"""Autonomous AI agent that resolves post-migration validation inconsistencies.

The repair-loop idea moved here from the Create Sync module: instead of starting
from failed plan applies, the agent starts from a validation report. For each
open inconsistency it asks the Foundation Model for remediation SQL (the same
structured-output contract and prompt context as the one-shot fixer), applies it
to Lakebase through the migration executor, and iterates on any Postgres error
until the object is consistent or attempts run out. Dependency chains resolve
across rounds — creating a function on round one can unblock the view that
references it on round two. The agent only converts source objects into the
target (missing/mismatch): row-count drift needs a data re-copy and extra
(target-only) objects need an explicit Remove from target, so both are marked
immediately with a pointer to the right action and never cost an FM call.

Runs on a daemon thread with an in-memory registry the API polls (same pattern
as backend/validation/runs.py).
"""
from __future__ import annotations

import logging
import threading
import uuid

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

from backend.config import FM_ENDPOINT
from backend.connectors.lakebase import LakebaseConnection
from backend.fm_params import chat_text, query_chat
from backend.migration.executor import apply_plan
from backend.migration.models import ItemStatus, ObjectKind, PlanItem
from backend.validation import fixer
from backend.validation.models import (
    MatchStatus,
    RepairAttempt,
    RepairItemState,
    RepairState,
    ValidationItem,
    ValidationRepairRequest,
)

log = logging.getLogger("lakebase_express.validation.agent")

_RUNS: dict[str, RepairState] = {}
_LOCK = threading.Lock()

_MAX_ERR_CHARS = 1500

_SYSTEM_PROMPT = """You are an autonomous database migration remediation agent. A \
post-migration validation compared a source Azure SQL / SQL Server database with its \
migrated Databricks Lakebase (PostgreSQL 15+) target and found inconsistencies. You \
resolve one inconsistency at a time by producing PostgreSQL SQL that is applied to \
the TARGET database immediately — no human review — so it must be complete and safe.

Rules:
- Object missing in the target: produce the complete CREATE statement, translating the
  provided T-SQL definition to PostgreSQL (LANGUAGE plpgsql for procedures/functions).
  A T-SQL trigger becomes a trigger function plus a CREATE TRIGGER statement — multiple
  statements are fine; they run in one transaction.
- Structural mismatch: produce ALTER TABLE statements that align the target table with
  the source columns. If a starting-point fix is provided, refine it rather than
  restarting from scratch.
- Keep the mapped schema and object name exactly as given. Always double-quote mapped
  schema/object identifiers so mixed-case names remain exact and case-sensitive. The
  statements run alone in one transaction and must be self-contained.
- If previous attempts are shown, they already failed: do NOT repeat them; use each
  Postgres error to refine the fix.
- "sql" must contain ONLY executable PostgreSQL statements — never prose, markdown
  fences, or JSON.
- If the inconsistency is genuinely unfixable by SQL, return an empty "sql" and say
  why in "analysis".

Respond with ONLY a JSON object with these keys, in this order:
  "analysis": a short diagnosis (2-4 sentences) of the inconsistency — or, on a retry,
              of why the last attempt failed — and what the SQL does;
  "sql": the complete Postgres SQL.
Think through "analysis" first, then produce "sql"."""


# --- Run registry -----------------------------------------------------------------


def get_repair(run_id: str) -> RepairState | None:
    with _LOCK:
        state = _RUNS.get(run_id)
        # Return a copy so the caller never observes a half-mutated object.
        return state.model_copy(deep=True) if state else None


def _register(req: ValidationRepairRequest) -> str:
    run_id = uuid.uuid4().hex[:12]
    state = RepairState(
        run_id=run_id,
        max_attempts=req.max_attempts,
        remaining=len(req.targets),
        items=[
            RepairItemState(
                id=t.item.id, name=t.item.target_name, kind=t.item.kind,
                reason=t.item.detail or f"{t.item.kind.value} is {t.item.status.value}",
                attempts=list(t.prior_attempts),  # continue an earlier run's history
            )
            for t in req.targets
        ],
    )
    with _LOCK:
        _RUNS[run_id] = state
    return run_id


def start_repair(req: ValidationRepairRequest) -> str:
    run_id = _register(req)
    threading.Thread(target=_execute, args=(run_id, req), daemon=True).start()
    return run_id


def _set(run_id: str, mutate) -> None:
    with _LOCK:
        state = _RUNS.get(run_id)
        if state:
            mutate(state)


# --- Foundation Model call --------------------------------------------------------


def _clip_err(text: str) -> str:
    return text if len(text) <= _MAX_ERR_CHARS else text[:_MAX_ERR_CHARS] + "\n… (truncated)"


# Kinds the comparator reports as one rollup item per table (constraints,
# indexes, foreign keys) rather than one item per object.
_ROLLUP_KINDS = frozenset({ObjectKind.CONSTRAINT, ObjectKind.INDEX, ObjectKind.FOREIGN_KEY})


def sql_fixable(item: ValidationItem) -> bool:
    """Whether the agent should resolve the inconsistency. Missing objects always
    have a CREATE fix; a mismatch does only when it is structural — pure row-count
    drift needs the data re-copied, not DDL. Extra (target-only) objects are never
    agent work: there is nothing to convert, so they are removed explicitly through
    the panel's Remove from target action instead."""
    if item.status is MatchStatus.MISSING:
        return True
    if item.status is MatchStatus.EXTRA:
        return False
    return bool(
        item.columns_missing or item.columns_extra or item.type_drift
        or item.collation_drift or item.fix_sql
    )


def _build_user_prompt(item: ValidationItem, state: RepairItemState, target_schema: str) -> str:
    # Same inconsistency context the one-shot fixer sends (source T-SQL,
    # structural diff, deterministic starting fix), plus this run's history.
    parts = [fixer._build_user_prompt(item, target_schema)]
    for a in state.attempts:
        if not a.sql.strip():
            continue  # model-call hiccup — nothing was applied, no signal to carry
        parts.append(
            f"Attempt {a.attempt} — your analysis: {a.analysis}\nSQL applied:\n"
            f"{fixer._clip(a.sql)}\n"
            f"Postgres error:\n{_clip_err(a.error or 'unknown')}"
        )
    if state.attempts:
        parts.append("Produce the corrected SQL.")
    return "\n\n".join(parts)


def _propose(
    item: ValidationItem, state: RepairItemState, target_schema: str, endpoint: str | None
) -> dict[str, str]:
    resp = query_chat(
        endpoint or FM_ENDPOINT,
        messages=[
            ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
            ChatMessage(role=ChatMessageRole.USER,
                        content=_build_user_prompt(item, state, target_schema)),
        ],
        temperature=0.0,
        max_tokens=fixer._MAX_OUTPUT_TOKENS,
        response_format=fixer._RESPONSE_FORMAT,
    )
    choice = resp.choices[0]
    # Raising keeps the item retryable: the loop records "Model call failed" and
    # tries again next round, instead of treating it as the agent giving up.
    if (choice.finish_reason or "").lower() == "length":
        raise RuntimeError("the model hit its output-token limit before finishing the fix")
    # chat_text flattens structured content blocks (reasoning models like
    # fable-5) down to the answer text; plain strings pass through.
    parsed = fixer._parse_proposal(chat_text(resp))
    if parsed is None:
        raise RuntimeError("the model returned malformed JSON instead of a fix")
    return {"analysis": parsed["analysis"], "sql": fixer._clean_sql(parsed["sql"])}


# --- Agent loop -------------------------------------------------------------------


def _execute(run_id: str, req: ValidationRepairRequest) -> None:
    try:
        conn = LakebaseConnection(**req.lakebase.conn_kwargs())
        items = {t.item.id: t.item for t in req.targets}
        state = get_repair(run_id)
        assert state is not None

        # Pre-pass: not every inconsistency is agent work — mark those up front
        # (no FM call) so the panel points the user at the right action: Re-copy
        # table data for row-count drift, Remove from target for extra objects.
        for idx, item_state in enumerate(state.items):
            item = items[item_state.id]
            if sql_fixable(item):
                continue
            if item.status is MatchStatus.EXTRA:
                analysis = ("This object exists only in the target — there is nothing "
                            "to convert. Review it and use Remove from target instead.")
                error = "Removal, not conversion — not agent work."
            elif item.kind in _ROLLUP_KINDS:
                # A constraint/index/FK rollup with no fix SQL has nothing
                # missing: every remaining entry is an extra object in the
                # target, which is a review-and-drop, not a conversion.
                analysis = (
                    f"Every {item.kind.value.replace('_', ' ')} the source defines is present. "
                    "The remaining differences are objects that exist only in Lakebase — "
                    "review them and drop any that should not be there."
                )
                error = "Target-only objects — removal, not conversion."
            elif item.kind is ObjectKind.TABLE and item.target_rows is None:
                analysis = ("The target row count could not be counted or estimated, so the "
                            "match is unverified — re-run the validation instead. If it "
                            "persists, check the table exists and is readable in the target.")
                error = "Unverified count — not fixable by SQL."
            else:
                analysis = ("Row-count drift is a data problem, not a DDL one — "
                            "use Re-copy table data for this table instead.")
                error = "Not fixable by SQL."
            _set(run_id, lambda s, idx=idx, analysis=analysis, error=error: (
                setattr(s.items[idx], "status", "failed"),
                setattr(s.items[idx], "gave_up", True),
                s.items[idx].attempts.append(RepairAttempt(
                    attempt=len(s.items[idx].attempts) + 1,
                    analysis=analysis, status="gave_up", error=error,
                )),
            ))
        state = get_repair(run_id)
        assert state is not None

        for attempt in range(1, req.max_attempts + 1):
            pending = [s for s in state.items if s.status != "success" and not s.gave_up]
            if not pending:
                break
            _set(run_id, lambda s, attempt=attempt: setattr(s, "attempt", attempt))

            # Phase 1 — ask the agent for a fix per open inconsistency.
            fixes: list[PlanItem] = []
            for item_state in pending:
                idx = next(i for i, s in enumerate(state.items) if s.id == item_state.id)
                _set(run_id, lambda s, idx=idx: setattr(s.items[idx], "status", "analyzing"))
                item = items[item_state.id]
                try:
                    fix = _propose(item, item_state, req.target_schema, req.endpoint)
                except Exception as exc:  # endpoint hiccup — item stays retryable
                    log.warning("Fix proposal failed for %s: %s", item.id, exc)
                    fix = {"analysis": f"Model call failed: {exc}", "sql": ""}
                # Numbered per item across runs, so seeded history stays in sequence.
                record = RepairAttempt(
                    attempt=len(item_state.attempts) + 1, analysis=fix["analysis"], sql=fix["sql"]
                )
                if not fix["sql"].strip():
                    # The pre-pass filtered genuinely unfixable items, so an empty
                    # "sql" here is the model stopping early — fail the attempt and
                    # let the next round push it again (bounded by max_attempts).
                    record.status = "failed"
                    record.error = "The agent did not produce a fix."
                    _set(run_id, lambda s, idx=idx, record=record: (
                        s.items[idx].attempts.append(record),
                        setattr(s.items[idx], "status", "failed"),
                    ))
                    continue
                _set(run_id, lambda s, idx=idx, record=record: (
                    s.items[idx].attempts.append(record),
                    setattr(s.items[idx], "status", "applying"),
                ))
                fixes.append(PlanItem(
                    id=item.id, kind=item.kind, name=item.target_name, sql=fix["sql"],
                    original=item.source_definition, reasoning=fix["analysis"],
                    notes="Resolved by the validation AI repair agent.",
                ))
            state = get_repair(run_id)
            assert state is not None
            if not fixes:
                continue  # nothing to apply (model hiccups) — retry or drain

            # Phase 2 — apply all fixes in one pass (executor orders by dependency kind).
            results = {r.id: r for r in apply_plan(conn, fixes)}
            for idx, item_state in enumerate(state.items):
                r = results.get(item_state.id)
                if not r or not item_state.attempts:
                    continue
                ok = r.status is ItemStatus.SUCCESS
                _set(run_id, lambda s, idx=idx, r=r, ok=ok: (
                    setattr(s.items[idx].attempts[-1], "status", "success" if ok else "failed"),
                    setattr(s.items[idx].attempts[-1], "error", r.error),
                    setattr(s.items[idx], "status", "success" if ok else "failed"),
                    setattr(s.items[idx], "fixed_sql", s.items[idx].attempts[-1].sql if ok else ""),
                ))
            state = get_repair(run_id)
            assert state is not None
            fixed = sum(s.status == "success" for s in state.items)
            _set(run_id, lambda s, fixed=fixed: (
                setattr(s, "fixed", fixed),
                setattr(s, "remaining", len(s.items) - fixed),
            ))

        state = get_repair(run_id)
        assert state is not None
        fixed = sum(s.status == "success" for s in state.items)
        final = "success" if fixed == len(state.items) else "partial" if fixed else "failed"
        _set(run_id, lambda s, final=final: setattr(s, "status", final))
    except Exception as exc:  # setup-level failure (e.g. bad connection)
        log.exception("Validation repair run %s failed during setup", run_id)
        _set(run_id, lambda s, exc=exc: (setattr(s, "status", "failed"), setattr(s, "error", str(exc))))
