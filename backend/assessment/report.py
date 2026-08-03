"""Assembles findings into a scored AssessmentReport."""
from __future__ import annotations

from collections import Counter

from backend.assessment.models import (
    AssessmentReport,
    Finding,
    ProgrammableObject,
    Severity,
    TableInfo,
)

# Penalty each finding deducts from a perfect 100 readiness score.
_PENALTY = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 4,
    Severity.HIGH: 10,
}


def _readiness_score(findings: list[Finding]) -> int:
    score = 100 - sum(_PENALTY[f.severity] for f in findings)
    return max(0, min(100, score))


def build_report(
    database: str,
    tables: list[TableInfo],
    objects: list[ProgrammableObject],
    findings: list[Finding],
) -> AssessmentReport:
    counts = Counter(f.severity.value for f in findings)
    return AssessmentReport(
        database=database,
        table_count=len(tables),
        total_rows=sum(t.row_count for t in tables),
        programmable_object_count=len(objects),
        findings=sorted(findings, key=lambda f: list(_PENALTY).index(f.severity), reverse=True),
        readiness_score=_readiness_score(findings),
        severity_counts={s.value: counts.get(s.value, 0) for s in Severity},
        tables=tables,
        programmable_objects=objects,
    )
