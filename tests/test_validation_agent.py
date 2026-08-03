"""Validation AI repair agent loop, with the Foundation Model and apply faked."""
import json
from types import SimpleNamespace

import pytest

from backend.migration.models import ItemResult, ItemStatus, LakebaseConnRequest, ObjectKind
from backend.validation import agent
from backend.validation.models import (
    MatchStatus,
    RepairAttempt,
    RepairTarget,
    ValidationItem,
    ValidationRepairRequest,
)

_CONN = LakebaseConnRequest(host="h", database="d", user="u", password="p")


def _item(**overrides) -> ValidationItem:
    base = dict(
        id="procedure:dbo.usp_Report", kind=ObjectKind.PROCEDURE,
        source_name="dbo.usp_Report", target_name="public.usp_report",
        status=MatchStatus.MISSING, detail="Procedure missing in the target.",
        source_definition="CREATE PROCEDURE dbo.usp_Report AS SELECT 1",
    )
    base.update(overrides)
    return ValidationItem(**base)


def _row_drift_item() -> ValidationItem:
    return _item(id="table:dbo.Orders", kind=ObjectKind.TABLE, source_name="dbo.Orders",
                 target_name="public.orders", status=MatchStatus.MISMATCH,
                 detail="Row counts differ.", source_definition="",
                 source_rows=100, target_rows=90)


def _req(items=None, max_attempts: int = 3) -> ValidationRepairRequest:
    targets = [RepairTarget(item=i) for i in (items or [_item()])]
    return ValidationRepairRequest(lakebase=_CONN, targets=targets, max_attempts=max_attempts)


def _fake_fix(analysis: str, sql: str):
    def fix(item, state, target_schema, endpoint):
        return {"analysis": analysis, "sql": sql}
    return fix


def _fake_apply(status_by_sql):
    """apply_plan double: looks up each item's outcome by its SQL."""
    def apply(conn, items, stop_on_error=False):
        return [
            ItemResult(id=i.id, name=i.name, kind=i.kind,
                       status=status_by_sql.get(i.sql, ItemStatus.FAILED),
                       error=None if status_by_sql.get(i.sql) is ItemStatus.SUCCESS else "still broken")
            for i in items
        ]
    return apply


def _run(req: ValidationRepairRequest) -> agent.RepairState:
    run_id = agent._register(req)
    agent._execute(run_id, req)
    return agent.get_repair(run_id)


@pytest.fixture(autouse=True)
def _no_real_connection(monkeypatch):
    monkeypatch.setattr(agent, "LakebaseConnection", lambda **k: SimpleNamespace(database="d"))


# --- Loop behavior ---------------------------------------------------------------


def test_fix_applied_on_first_attempt(monkeypatch):
    good = "CREATE PROCEDURE public.usp_report() LANGUAGE plpgsql AS $$ BEGIN END; $$;"
    monkeypatch.setattr(agent, "_propose", _fake_fix("Translate the T-SQL procedure.", good))
    monkeypatch.setattr(agent, "apply_plan", _fake_apply({good: ItemStatus.SUCCESS}))

    state = _run(_req())
    assert state.status == "success" and state.fixed == 1 and state.remaining == 0
    item = state.items[0]
    assert item.status == "success" and item.fixed_sql == good
    assert item.reason == "Procedure missing in the target."
    assert len(item.attempts) == 1
    assert item.attempts[0].status == "success" and item.attempts[0].analysis


def test_agent_iterates_and_stops_at_max_attempts(monkeypatch):
    calls: list[int] = []

    def fix(item, state, target_schema, endpoint):
        calls.append(len(state.attempts))  # history grows every round
        return {"analysis": f"try {len(state.attempts) + 1}", "sql": f"SQL v{len(state.attempts) + 1}"}

    monkeypatch.setattr(agent, "_propose", fix)
    monkeypatch.setattr(agent, "apply_plan", _fake_apply({}))  # everything keeps failing

    state = _run(_req(max_attempts=3))
    assert state.status == "failed" and state.fixed == 0
    item = state.items[0]
    assert item.status == "failed" and not item.gave_up
    assert len(item.attempts) == 3
    assert [a.error for a in item.attempts] == ["still broken"] * 3
    assert calls == [0, 1, 2]  # each round saw the previous attempts


def test_row_count_drift_is_flagged_for_recopy_without_an_fm_call(monkeypatch):
    monkeypatch.setattr(
        agent, "_propose",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("row drift must not reach the model")),
    )
    monkeypatch.setattr(
        agent, "apply_plan",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing must be applied")),
    )

    state = _run(_req(items=[_row_drift_item()]))
    item = state.items[0]
    assert state.status == "failed" and state.error is None
    assert item.gave_up and item.status == "failed"
    assert len(item.attempts) == 1 and item.attempts[0].status == "gave_up"
    assert "Re-copy" in item.attempts[0].analysis


def test_extra_object_is_flagged_for_removal_without_an_fm_call(monkeypatch):
    # Extras are removals, not conversions — they never reach the agent's loop.
    monkeypatch.setattr(
        agent, "_propose",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("extras must not reach the model")),
    )
    monkeypatch.setattr(
        agent, "apply_plan",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing must be applied")),
    )

    extra = _item(id="extra-table:public.zombie", kind=ObjectKind.TABLE, source_name="",
                  target_name="public.zombie", status=MatchStatus.EXTRA,
                  detail="Exists only in Lakebase.", source_definition="")
    state = _run(_req(items=[extra]))
    item = state.items[0]
    assert state.status == "failed" and state.error is None
    assert item.gave_up and item.status == "failed"
    assert len(item.attempts) == 1 and item.attempts[0].status == "gave_up"
    assert "Remove from target" in item.attempts[0].analysis


def test_empty_sql_for_a_fixable_item_is_retried_not_given_up(monkeypatch):
    # The pre-pass filtered genuinely unfixable items, so an empty "sql" for a
    # missing object is the model stopping early — the next round pushes again.
    replies = iter([
        {"analysis": "explains but no SQL", "sql": " "},
        {"analysis": "created", "sql": "good sql"},
    ])
    monkeypatch.setattr(agent, "_propose", lambda *a, **k: next(replies))
    monkeypatch.setattr(agent, "apply_plan", _fake_apply({"good sql": ItemStatus.SUCCESS}))

    state = _run(_req(max_attempts=3))
    item = state.items[0]
    assert state.status == "success" and item.status == "success"
    assert not item.gave_up
    assert [a.status for a in item.attempts] == ["failed", "success"]
    assert item.attempts[0].error == "The agent did not produce a fix."


def test_partial_when_one_item_stays_broken(monkeypatch):
    other = _item(id="view:dbo.v", kind=ObjectKind.VIEW, source_name="dbo.v",
                  target_name="public.v", detail="View missing.",
                  source_definition="CREATE VIEW dbo.v AS SELECT 1")
    req = _req(items=[_item(), other], max_attempts=2)
    monkeypatch.setattr(
        agent, "_propose",
        lambda item, state, target_schema, endpoint: {"analysis": "x", "sql": f"fixed {item.id}"},
    )
    monkeypatch.setattr(agent, "apply_plan", _fake_apply({"fixed view:dbo.v": ItemStatus.SUCCESS}))

    state = _run(req)
    assert state.status == "partial" and state.fixed == 1 and state.remaining == 1
    by_id = {i.id: i for i in state.items}
    assert by_id["view:dbo.v"].status == "success"
    assert by_id["procedure:dbo.usp_Report"].status == "failed"
    assert len(by_id["procedure:dbo.usp_Report"].attempts) == 2


def test_model_call_failure_keeps_item_retryable(monkeypatch):
    attempts = {"n": 0}

    def flaky(item, state, target_schema, endpoint):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("endpoint timeout")
        return {"analysis": "ok now", "sql": "good sql"}

    monkeypatch.setattr(agent, "_propose", flaky)
    monkeypatch.setattr(agent, "apply_plan", _fake_apply({"good sql": ItemStatus.SUCCESS}))

    state = _run(_req(max_attempts=3))
    item = state.items[0]
    assert state.status == "success" and item.status == "success"
    assert not item.gave_up
    assert [a.status for a in item.attempts] == ["failed", "success"]


def test_rerun_continues_from_prior_attempts(monkeypatch):
    """A second agent run seeds the first run's attempts: the model sees the
    full history (so it won't repeat itself) and numbering carries on."""
    prior = RepairAttempt(attempt=1, analysis="tried plain CREATE", sql="try1",
                          status="failed", error="err1")
    req = ValidationRepairRequest(
        lakebase=_CONN, targets=[RepairTarget(item=_item(), prior_attempts=[prior])],
        max_attempts=2,
    )

    prompts: list[str] = []

    def fix(item, state, target_schema, endpoint):
        prompts.append(agent._build_user_prompt(item, state, target_schema))
        return {"analysis": "continue", "sql": "good sql"}

    monkeypatch.setattr(agent, "_propose", fix)
    monkeypatch.setattr(agent, "apply_plan", _fake_apply({"good sql": ItemStatus.SUCCESS}))

    state = _run(req)
    item = state.items[0]
    assert state.status == "success"
    # History = seeded attempt + this run's fix, numbered consecutively.
    assert [(a.attempt, a.sql) for a in item.attempts] == [(1, "try1"), (2, "good sql")]
    # The prompt carried the earlier run's attempt so the agent continues from it.
    assert "try1" in prompts[0] and "err1" in prompts[0] and "tried plain CREATE" in prompts[0]


# --- Prompt & proposal -----------------------------------------------------------


def test_prompt_carries_inconsistency_and_attempt_history():
    item = _item(fix_sql="CREATE PROCEDURE ... (starting point)")
    state = agent.RepairItemState(
        id=item.id, name=item.target_name, kind=item.kind, reason=item.detail,
        attempts=[RepairAttempt(attempt=1, analysis="a1", sql="try1", status="failed", error="err1")],
    )
    prompt = agent._build_user_prompt(item, state, "public")
    assert "public.usp_report" in prompt and "dbo.usp_Report" in prompt
    assert "CREATE PROCEDURE dbo.usp_Report" in prompt        # source T-SQL context
    assert "starting point" in prompt                          # deterministic seed
    assert "Attempt 1" in prompt and "try1" in prompt and "err1" in prompt
    assert "Produce the corrected SQL." in prompt


def test_sql_fixable_matches_the_fix_semantics():
    assert agent.sql_fixable(_item())                                              # missing
    assert not agent.sql_fixable(_item(status=MatchStatus.EXTRA, source_name=""))  # removal, not conversion
    assert not agent.sql_fixable(_row_drift_item())                                # rows only
    assert agent.sql_fixable(_row_drift_item().model_copy(update={"columns_missing": ["Total"]}))


def _resp(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason,
    )])


def test_propose_requests_structured_output_and_cleans_sql(monkeypatch):
    sent = {}

    def fake(endpoint, messages, **params):
        sent.update(params)
        return _resp(json.dumps({"analysis": "ok", "sql": "```sql\nSELECT 1;\n```"}))

    monkeypatch.setattr(agent, "query_chat", fake)
    state = agent.RepairItemState(id="x", name="public.x", kind=ObjectKind.PROCEDURE)
    fix = agent._propose(_item(), state, "public", None)
    assert fix == {"analysis": "ok", "sql": "SELECT 1;"}
    # Same structured-output contract as the one-shot fixer.
    assert sent["response_format"]["type"] == "json_schema"
    schema = sent["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"analysis", "sql"}


def test_propose_handles_structured_content_blocks(monkeypatch):
    # Reasoning models (e.g. fable-5) return message.content as a list of
    # blocks; chat_text keeps only the text parts.
    blocks = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking…"}]},
        {"type": "text", "text": '{"analysis": "ok", "sql": "SELECT 1;"}'},
    ]
    monkeypatch.setattr(agent, "query_chat", lambda endpoint, messages, **p: _resp(blocks))
    state = agent.RepairItemState(id="x", name="public.x", kind=ObjectKind.PROCEDURE)
    fix = agent._propose(_item(), state, "public", None)
    assert fix == {"analysis": "ok", "sql": "SELECT 1;"}


def test_propose_raises_on_truncation_and_malformed_json(monkeypatch):
    state = agent.RepairItemState(id="x", name="public.x", kind=ObjectKind.PROCEDURE)
    monkeypatch.setattr(agent, "query_chat",
                        lambda endpoint, messages, **p: _resp('{"analysis": "x", "sql": "CRE', "length"))
    with pytest.raises(RuntimeError, match="token limit"):
        agent._propose(_item(), state, "public", None)

    monkeypatch.setattr(agent, "query_chat",
                        lambda endpoint, messages, **p: _resp('{"analysis": "x", "sql": "CRE'))
    with pytest.raises(RuntimeError, match="malformed JSON"):
        agent._propose(_item(), state, "public", None)
