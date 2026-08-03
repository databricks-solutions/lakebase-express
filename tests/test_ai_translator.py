"""T-SQL -> PL/pgSQL translator: structured output, parsing, fail-soft behavior."""
import json
from types import SimpleNamespace

from backend.assessment.models import ProgrammableObject
from backend.schema_migration import ai_translator


def _obj(definition="CREATE PROCEDURE dbo.usp_Report AS SELECT 1"):
    return ProgrammableObject(schema_name="dbo", object_name="usp_Report",
                              object_type="PROCEDURE", line_count=1, definition=definition)


def _resp(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason,
    )])


# --- _parse_payload ---------------------------------------------------------------------


def test_parse_payload_handles_json_fences_and_prose_fallback():
    payload = {"reasoning": "swap TOP for LIMIT", "translated": "SELECT 1", "notes": "none"}
    assert ai_translator._parse_payload(json.dumps(payload)) == payload
    assert ai_translator._parse_payload(f"Sure:\n```json\n{json.dumps(payload)}\n```") == payload
    # A reply with no JSON shape at all is plausibly bare SQL — kept, flagged for review.
    out = ai_translator._parse_payload("CREATE VIEW v AS SELECT 1")
    assert out["translated"] == "CREATE VIEW v AS SELECT 1" and "review" in out["notes"]


def test_parse_payload_tolerates_raw_newlines_inside_strings():
    content = ('{\n  "reasoning": "translated the proc",\n  "translated": "CREATE PROCEDURE '
               'public.usp_report()\nLANGUAGE plpgsql\nAS $$\nBEGIN\nEND;\n$$;",\n  "notes": ""\n}')
    out = ai_translator._parse_payload(content)
    assert out is not None and "CREATE PROCEDURE public.usp_report()" in out["translated"]


def test_parse_payload_rejects_json_shaped_garbage():
    # A truncated JSON reply used to land verbatim in the plan's SQL editor.
    truncated = '{\n  "reasoning": "translating...",\n  "translated": "\nCREATE PROCEDURE'
    assert ai_translator._parse_payload(truncated) is None


# --- translate_object --------------------------------------------------------------------


def test_translate_object_requests_structured_output(monkeypatch):
    sent = {}

    def fake(endpoint, messages, **params):
        sent.update(params)
        return _resp(json.dumps({"reasoning": "r", "translated": "SELECT 1", "notes": "n"}))

    monkeypatch.setattr(ai_translator, "query_chat", fake)
    tr = ai_translator.translate_object(_obj(), endpoint="my-endpoint")
    assert tr.success and tr.translated == "SELECT 1" and tr.reasoning == "r"
    # Structured output constrains the reply to {"reasoning", "translated", "notes"}.
    assert sent["response_format"]["type"] == "json_schema"
    schema = sent["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"reasoning", "translated", "notes"}
    # Long procedure bodies must fit — 4000 used to truncate mid-"translated".
    assert sent["max_tokens"] >= 8000


def test_translate_object_reports_token_limit_truncation(monkeypatch):
    monkeypatch.setattr(ai_translator, "query_chat",
                        lambda endpoint, messages, **p: _resp('{"reasoning": "x", "translated": "CRE', "length"))
    tr = ai_translator.translate_object(_obj())
    assert not tr.success and tr.translated == "" and "token limit" in tr.notes


def test_translate_object_reports_malformed_json_instead_of_dumping_it(monkeypatch):
    monkeypatch.setattr(ai_translator, "query_chat",
                        lambda endpoint, messages, **p: _resp('{"reasoning": "x", "translated": "CRE'))
    tr = ai_translator.translate_object(_obj())
    assert not tr.success and tr.translated == "" and "malformed" in tr.notes


def test_translate_object_is_fail_soft(monkeypatch):
    def boom(endpoint, messages, **params):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(ai_translator, "query_chat", boom)
    tr = ai_translator.translate_object(_obj())
    assert not tr.success and "endpoint down" in tr.notes


# --- Prompt guidance --------------------------------------------------------------------


def test_prompt_schema_qualifies_regular_objects():
    prompt = ai_translator._build_user_prompt(_obj(), schema_map={"dbo": "public"})
    assert 'Create this object as "public"."usp_report"' in prompt


def test_prompt_preserves_exact_case_when_requested():
    obj = ProgrammableObject(
        schema_name="SalesLT", object_name="GetProducts", object_type="PROCEDURE",
        line_count=1, definition="x",
    )
    prompt = ai_translator._build_user_prompt(
        obj, schema_map={"SalesLT": "SalesLT"}, identifier_case="preserve"
    )
    assert 'Create this object as "SalesLT"."GetProducts"' in prompt
    assert "Preserve the exact source casing" in prompt
    assert "double-quote every identifier" in prompt


def test_prompt_never_schema_qualifies_trigger_names():
    """CREATE TRIGGER schema.name is a Postgres syntax error (triggers are
    namespaced by their table) — the guidance must put the schema on the ON
    clause and the trigger function, not the trigger name."""
    trg = ProgrammableObject(schema_name="Trading", object_name="trg_order_audit",
                             object_type="TRIGGER", line_count=1, definition="x")
    prompt = ai_translator._build_user_prompt(trg, schema_map={"Trading": "trading"})
    assert "do NOT schema-qualify the trigger name" in prompt
    assert 'Create this object as "trading"."trg_order_audit"' not in prompt
    assert "CREATE OR REPLACE TRIGGER trg_order_audit" in prompt
    assert "EXECUTE FUNCTION trading.trg_order_audit_fn()" in prompt


# --- Deterministic trigger sanitizer (unit tests live in test_trigger_sql.py) -----------


def test_translate_object_sanitizes_only_triggers(monkeypatch):
    payload = ('{"reasoning":"r","translated":'
               '"CREATE TRIGGER app.t AFTER INSERT ON app.x FOR EACH ROW EXECUTE FUNCTION app.t();",'
               '"notes":"n"}')
    monkeypatch.setattr(ai_translator, "query_chat", lambda *a, **k: _resp(payload))
    trg = ProgrammableObject(schema_name="app", object_name="t", object_type="TRIGGER",
                             line_count=1, definition="x")
    tr = ai_translator.translate_object(trg)
    assert tr.success and "CREATE OR REPLACE TRIGGER t AFTER" in tr.translated
    assert "TRIGGER app.t AFTER" not in tr.translated
    # A non-trigger object with a "CREATE TRIGGER"-shaped string is left alone.
    proc = ai_translator.translate_object(_obj())
    assert proc.success


# --- translate_all (concurrent) ---------------------------------------------------------


def test_translate_all_preserves_order_and_reports_progress(monkeypatch):
    # Each object gets a translation echoing its name, so we can assert the
    # result order matches the input order despite concurrent execution.
    def fake(endpoint, messages, **params):
        body = messages[-1].content
        marker = "obj7" if "obj7" in body else "objX"
        return _resp(json.dumps({"reasoning": "r", "translated": f"-- {marker}", "notes": "n"}))

    monkeypatch.setattr(ai_translator, "query_chat", fake)
    objs = [ProgrammableObject(schema_name="dbo", object_name=f"obj{i}",
                               object_type="PROCEDURE", line_count=1,
                               definition=f"CREATE PROCEDURE dbo.obj{i} AS SELECT 1")
            for i in range(10)]

    seen: list[tuple[int, int]] = []
    out = ai_translator.translate_all(objs, on_done=lambda d, t: seen.append((d, t)))

    assert [t.object_name for t in out] == [f"dbo.obj{i}" for i in range(10)]  # order preserved
    assert out[7].translated == "-- obj7"                                      # right result per slot
    assert len(seen) == 10 and seen[-1] == (10, 10)                            # progress to completion
    assert {t for _, t in seen} == {10}                                        # total constant


def test_translate_all_empty_is_noop():
    assert ai_translator.translate_all([]) == []
