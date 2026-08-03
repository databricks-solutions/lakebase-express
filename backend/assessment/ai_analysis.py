"""AI migration analysis — a Foundation Model deep-dive over the scan results.

This augments (never replaces) the deterministic scan in ``compatibility.py`` /
``report.py``. The rule engine gives the factual readiness score and per-object
findings; this layer asks a Databricks Foundation Model to reason over the same
data and surface deeper *semantic / behavioral / operational* risks a regex pass
can't see — transaction & isolation semantics, identity/sequence behavior,
collation & case sensitivity, datetime precision, implicit conversions, and
cross-object dependencies.

It is intentionally fail-soft: any error returns an ``AIAssessment`` with
``success=False`` so the deterministic assessment is always returned intact.
"""
from __future__ import annotations

import functools
import json
import logging
import re
from collections import Counter
from pathlib import Path

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
from jinja2 import Environment, FileSystemLoader

from backend.assessment.models import (
    AIAssessment,
    AIRisk,
    AssessmentReport,
)
from backend.config import FM_ENDPOINT
from backend.fm_params import chat_text, query_chat

log = logging.getLogger("lakebase_express.ai_analysis")

# Keep the prompt bounded regardless of source size.
_MAX_TABLES = 40
_MAX_OBJECTS = 20
_MAX_DEF_CHARS = 1200

# Reasoning models spend thinking tokens from the same completion budget, so a
# small cap truncates the JSON answer mid-array. Ask big; query_chat clamps to
# the endpoint's actual output window.
_MAX_OUTPUT_TOKENS = 128000

# Structured output schema: constrains the endpoint to emit exactly the
# assessment shape, instead of pretty-printed JSON with raw newlines or prose
# around it. Endpoints that reject the parameter degrade gracefully to
# free-form output (see fm_params.query_chat).
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "migration_analysis",
        "schema": {
            "type": "object",
            "properties": {
                "complexity": {"type": "string", "enum": ["Low", "Medium", "High"]},
                "complexity_rationale": {"type": "string"},
                "summary": {"type": "string"},
                "risks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "category": {"type": "string"},
                            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                            "affected_objects": {"type": "string"},
                            "rationale": {"type": "string"},
                            "recommendation": {"type": "string"},
                        },
                        "required": [
                            "title", "category", "severity",
                            "affected_objects", "rationale", "recommendation",
                        ],
                        "additionalProperties": False,
                    },
                },
                "recommendations": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "complexity", "complexity_rationale", "summary", "risks", "recommendations",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

# Prompts live as Jinja templates in ./prompts so they're easy to read and tune
# without touching Python.
_PROMPT_DIR = Path(__file__).parent / "prompts"


@functools.lru_cache(maxsize=1)
def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_PROMPT_DIR)),
        autoescape=False,  # prompts are plain text, not HTML
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _system_prompt() -> str:
    return _jinja_env().get_template("migration_analysis.system.jinja").render().strip()


def _context(report: AssessmentReport) -> str:
    """Compact, token-bounded textual summary of the scan for the model."""
    lines: list[str] = [
        f"Database: {report.database}",
        f"Tables: {report.table_count} | Total rows: {report.total_rows:,} | "
        f"Programmable objects: {report.programmable_object_count}",
        f"Deterministic readiness score: {report.readiness_score}/100",
        "",
        "TABLES (name · rows · column data types):",
    ]
    for t in report.tables[:_MAX_TABLES]:
        types = ", ".join(sorted({c.data_type for c in t.columns}))
        lines.append(f"- {t.fqn} · {t.row_count:,} rows · [{types}]")
    if report.table_count > _MAX_TABLES:
        lines.append(f"  …and {report.table_count - _MAX_TABLES} more tables")

    lines += ["", "PROGRAMMABLE OBJECTS (T-SQL, truncated):"]
    if not report.programmable_objects:
        lines.append("- none")
    for o in report.programmable_objects[:_MAX_OBJECTS]:
        body = o.definition[:_MAX_DEF_CHARS]
        if len(o.definition) > _MAX_DEF_CHARS:
            body += "\n-- …truncated…"
        lines.append(f"\n### {o.object_type} {o.schema_name}.{o.object_name} ({o.line_count} lines)\n{body}")
    if report.programmable_object_count > _MAX_OBJECTS:
        lines.append(f"\n…and {report.programmable_object_count - _MAX_OBJECTS} more objects")

    # Rule findings as a histogram so the model knows what the scan already caught.
    if report.findings:
        hist = Counter(f"{f.severity.value}:{f.title}" for f in report.findings)
        lines += ["", "DETERMINISTIC RULE FINDINGS (severity:title × count):"]
        lines += [f"- {k} × {n}" for k, n in hist.most_common()]
    else:
        lines += ["", "DETERMINISTIC RULE FINDINGS: none"]

    return "\n".join(lines)


def _extract_json(content: str) -> dict:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            candidate = candidate[start : end + 1]
    # strict=False tolerates raw newlines inside string values, a common model slip.
    return json.loads(candidate, strict=False)


def analyze_migration(report: AssessmentReport, endpoint: str | None = None) -> AIAssessment:
    """Run the Foundation Model deep-dive. Never raises — returns success=False on error."""
    endpoint = endpoint or FM_ENDPOINT
    try:
        resp = query_chat(
            endpoint,
            messages=[
                ChatMessage(role=ChatMessageRole.SYSTEM, content=_system_prompt()),
                ChatMessage(role=ChatMessageRole.USER, content=_context(report)),
            ],
            temperature=0.2,
            max_tokens=_MAX_OUTPUT_TOKENS,
            response_format=_RESPONSE_FORMAT,
        )
        if (resp.choices[0].finish_reason or "").lower() == "length":
            return AIAssessment(
                endpoint=endpoint,
                success=False,
                error="The model hit its output-token limit before finishing the analysis — "
                      "re-run it, or pick an endpoint with a larger output window in Settings.",
            )
        data = _extract_json(chat_text(resp))
        risks = [
            AIRisk(
                title=str(r.get("title", "")).strip(),
                category=str(r.get("category", "")).strip(),
                severity=str(r.get("severity", "medium")).strip().lower(),
                affected_objects=str(r.get("affected_objects", "")).strip(),
                rationale=str(r.get("rationale", "")).strip(),
                recommendation=str(r.get("recommendation", "")).strip(),
            )
            for r in data.get("risks", [])
            if isinstance(r, dict) and r.get("title")
        ]
        return AIAssessment(
            summary=str(data.get("summary", "")).strip(),
            complexity=str(data.get("complexity", "Medium")).strip().title(),
            complexity_rationale=str(data.get("complexity_rationale", "")).strip(),
            estimated_effort=str(data.get("estimated_effort", "")).strip(),
            risks=risks,
            recommendations=[str(x).strip() for x in data.get("recommendations", []) if str(x).strip()],
            endpoint=endpoint,
            success=True,
        )
    except Exception as exc:  # fail soft — deterministic assessment must still return
        log.exception("AI migration analysis failed (endpoint=%s)", endpoint)
        return AIAssessment(endpoint=endpoint, success=False, error=str(exc))
