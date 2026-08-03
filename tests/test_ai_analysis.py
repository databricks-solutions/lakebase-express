"""AI migration analysis: structured output, tolerant parsing, fail-soft behavior."""
import json
from types import SimpleNamespace

from backend.assessment import ai_analysis
from backend.assessment.models import AssessmentReport


def _report():
    return AssessmentReport(
        database="appdb", table_count=0, total_rows=0, programmable_object_count=0,
        findings=[], readiness_score=90, severity_counts={},
        tables=[], programmable_objects=[],
    )


def _resp(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason,
    )])


def _payload():
    return {
        "complexity": "Low",
        "complexity_rationale": "small schema",
        "summary": "Trivial migration.",
        "risks": [{
            "title": "Collation case sensitivity",
            "category": "schema",
            "severity": "medium",
            "affected_objects": "multiple",
            "rationale": "PG is case-sensitive",
            "recommendation": "use citext",
        }],
        "recommendations": ["run the schema migration"],
    }


# --- _extract_json ----------------------------------------------------------------------


def test_extract_json_handles_fences_and_surrounding_prose():
    payload = _payload()
    assert ai_analysis._extract_json(json.dumps(payload)) == payload
    assert ai_analysis._extract_json(f"Sure:\n```json\n{json.dumps(payload)}\n```") == payload
    assert ai_analysis._extract_json(f"Here you go: {json.dumps(payload)} — done.") == payload


def test_extract_json_tolerates_raw_newlines_inside_strings():
    content = '{\n  "summary": "line one\nline two",\n  "risks": []\n}'
    assert ai_analysis._extract_json(content)["summary"] == "line one\nline two"


# --- analyze_migration ------------------------------------------------------------------


def test_analyze_migration_requests_structured_output(monkeypatch):
    sent = {}

    def fake(endpoint, messages, **params):
        sent.update(params)
        return _resp(json.dumps(_payload()))

    monkeypatch.setattr(ai_analysis, "query_chat", fake)
    out = ai_analysis.analyze_migration(_report(), endpoint="my-endpoint")
    assert out.success and out.complexity == "Low" and out.risks[0].severity == "medium"
    # Structured output constrains the reply to the assessment shape.
    assert sent["response_format"]["type"] == "json_schema"
    schema = sent["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {
        "complexity", "complexity_rationale", "summary", "risks", "recommendations",
    }
    # Reasoning models spend thinking tokens from this budget — 2000 used to
    # truncate the JSON mid-array on databricks-claude-opus-4-8.
    assert sent["max_tokens"] >= 8000


def test_analyze_migration_reports_token_limit_truncation(monkeypatch):
    monkeypatch.setattr(ai_analysis, "query_chat",
                        lambda endpoint, messages, **p: _resp('{"summary": "trunc', "length"))
    out = ai_analysis.analyze_migration(_report(), endpoint="my-endpoint")
    assert not out.success and "token limit" in out.error


def test_analyze_migration_is_fail_soft(monkeypatch):
    def boom(endpoint, messages, **params):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(ai_analysis, "query_chat", boom)
    out = ai_analysis.analyze_migration(_report(), endpoint="my-endpoint")
    assert not out.success and "endpoint down" in out.error
