"""Builds a MigrationReport from a persisted project plus its run history.

Opens no source or target connection and calls no model: every number is a
projection of what the phases already stored. The one read beyond the project row
is the run store, which is where what the migration actually *did* is recorded —
and ``scope="assessment"`` skips even that.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from backend.assessment import callable_shape
from backend.assessment.models import AssessmentReport
from backend.migration.models import POST_DATA_KINDS, ObjectKind, PlanItem
from backend.projects.models import Project
from backend.query_parity.models import QueryParityReport
from backend.report.models import (
    SCOPE_ASSESSMENT,
    SCOPE_FULL,
    AssessmentSection,
    CodeObjectRow,
    Coordinates,
    FindingGroup,
    Headline,
    LoadRun,
    LoadTable,
    MigrationReport,
    ParityRow,
    ParitySection,
    PlanSection,
    ReportProvenance,
    ResultSection,
    TableRow,
    ValidationRow,
    ValidationSection,
)
from backend.run_store import MemoryRunStore, get_run_store
from backend.validation.models import MatchStatus, ValidationReport

# Caps on the enumerations a wide database makes unbounded. Every capped list also
# reports its true size, so a trimmed one is never mistaken for a complete one.
_AFFECTED_CAP = 25
_TABLE_CAP = 15
_CODE_OBJECT_CAP = 150
_OUTSTANDING_CAP = 150
_DIFF_CAP = 12
# Runs whose payload is read and rendered, out of the recent history that is listed —
# listing is payload-free, so the wider horizon is what lets a trimmed list say so.
_RUN_CAP = 10
_RUN_HORIZON = 50

_LOAD_KINDS = ("sync_run", "async_run", "async_job")
_CODE_KINDS = frozenset(
    {ObjectKind.PROCEDURE, ObjectKind.VIEW, ObjectKind.FUNCTION, ObjectKind.TRIGGER}
)
# Table statuses whose rows are in the target.
_TABLE_DONE = frozenset({"success", "skipped"})

# Driver errors reach an artifact built to be shared, and Postgres appends DETAIL lines
# to a constraint violation that name the offending key values — or print the whole
# failing row. Only the primary message travels; the app still shows the full text.
_ERROR_CAP = 200

_SCORE_FORMULA = (
    "100 minus 10 per high, 4 per medium and 1 per low finding, clamped to 0-100. "
    "The clamp means many low findings and no high ones can still read 0, so read it "
    "with the severity counts beside it."
)


# --- Parsing the stored row -------------------------------------------------------


def _assessment(project: Project) -> AssessmentReport | None:
    if not project.assessment:
        return None
    try:
        return AssessmentReport.model_validate(project.assessment)
    except Exception:
        return None  # a report from an older shape must not break the export


def _plan(project: Project) -> list[PlanItem]:
    items: list[PlanItem] = []
    for raw in project.plan or []:
        try:
            items.append(PlanItem.model_validate(raw))
        except Exception:
            continue
    return items


def _validation(project: Project) -> ValidationReport | None:
    if not project.validation:
        return None
    try:
        return ValidationReport.model_validate(project.validation)
    except Exception:
        return None


def _parity(project: Project) -> QueryParityReport | None:
    if not project.query_parity:
        return None
    try:
        return QueryParityReport.model_validate(project.query_parity)
    except Exception:
        return None


# --- Sections ---------------------------------------------------------------------


def _coordinates(project: Project) -> Coordinates:
    ic = project.identifier_case
    return Coordinates(
        source_type=project.source.source_type,
        source_host=project.source.host,
        source_database=project.source.database,
        target_host=project.target.host,
        target_database=project.target.database,
        target_schema=project.target_schema,
        identifier_case=ic.value if hasattr(ic, "value") else str(ic),
    )


def _assessment_section(report: AssessmentReport) -> AssessmentSection:
    grouped: dict[str, list] = {}
    for finding in report.findings:
        grouped.setdefault(finding.rule_id, []).append(finding)

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings: list[FindingGroup] = []
    for rule_id, group in grouped.items():
        first = group[0]
        affected = sorted({f.object_name for f in group})
        findings.append(
            FindingGroup(
                rule_id=rule_id,
                title=first.title,
                severity=first.severity.value,
                detail=first.detail,
                recommendation=first.recommendation,
                affected=affected[:_AFFECTED_CAP],
                affected_total=len(affected),
            )
        )
    findings.sort(key=lambda f: (order.get(f.severity, 9), -f.affected_total, f.rule_id))

    tables = sorted(report.tables, key=lambda t: t.row_count, reverse=True)
    ai = report.ai_assessment if (report.ai_assessment and report.ai_assessment.success) else None
    return AssessmentSection(
        database=report.database,
        readiness_score=report.readiness_score,
        score_formula=_SCORE_FORMULA,
        severity_counts=dict(report.severity_counts),
        table_count=report.table_count,
        total_rows=report.total_rows,
        programmable_object_count=report.programmable_object_count,
        object_counts=dict(
            Counter(o.object_type.upper() for o in report.programmable_objects)
        ),
        largest_tables=[
            TableRow(
                name=t.fqn,
                rows=t.row_count,
                columns=t.column_count,
                primary_key=", ".join(t.primary_key),
            )
            for t in tables[:_TABLE_CAP]
        ],
        tables_total=len(report.tables),
        findings=findings,
        findings_total=len(report.findings),
        ai=ai,
    )


def _plan_section(plan: list[PlanItem]) -> PlanSection:
    code = [i for i in plan if i.kind in _CODE_KINDS]
    rows: list[CodeObjectRow] = []
    for item in code:
        translated = bool(item.sql.strip())
        provenance = (
            "not-translated" if not translated
            else "ai" if item.reasoning.strip() else "user-edited"
        )
        rows.append(
            CodeObjectRow(
                source=item.id.split(":", 1)[1] if ":" in item.id else item.id,
                target=item.name,
                object_type=item.kind.value.upper(),
                target_kind=callable_shape.target_kind(item.sql),
                provenance=provenance,
                note=item.notes,
            )
        )
    return PlanSection(
        total=len(plan),
        by_kind=dict(Counter(i.kind.value for i in plan)),
        pre_data=sum(1 for i in plan if i.kind not in POST_DATA_KINDS),
        post_data=sum(1 for i in plan if i.kind in POST_DATA_KINDS),
        collations=[i.name for i in plan if i.kind is ObjectKind.COLLATION],
        code_objects=rows[:_CODE_OBJECT_CAP],
        code_objects_total=len(rows),
        translated=sum(1 for r in rows if r.provenance == "ai"),
        user_edited=sum(1 for r in rows if r.provenance == "user-edited"),
        not_translated=sum(1 for r in rows if r.provenance == "not-translated"),
    )


def _duration(started: str | None, finished: str | None) -> str:
    """"4m 12s" between two ISO stamps, or "" when either is missing or unparsable."""
    if not started or not finished:
        return ""
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished)
    except ValueError:
        return ""
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _error(text: object) -> str:
    """A driver error's first line only, capped — never its DETAIL or HINT."""
    lines = str(text or "").strip().splitlines()
    if not lines:
        return ""
    message = " ".join(lines[0].split())
    if len(message) > _ERROR_CAP:
        return message[:_ERROR_CAP].rstrip() + "…"
    return message


def _rows_landed(tables) -> int:
    """Rows actually in the target. A failed table is copied in one transaction, so its
    progress-before-failure was rolled back and must not be counted."""
    return sum(
        int(t.get("rows_copied") or 0) for t in tables if t.get("status") in _TABLE_DONE
    )


def _sync_run(data: dict) -> LoadRun:
    tables = data.get("tables") or []
    return LoadRun(
        kind="sync_run",
        run_id=str(data.get("run_id", "")),
        status=str(data.get("status", "")),
        started_at=str(data.get("started_at") or ""),
        finished_at=str(data.get("finished_at") or ""),
        duration=_duration(data.get("started_at"), data.get("finished_at")),
        tables_total=len(tables),
        tables_ok=sum(1 for t in tables if t.get("status") == "success"),
        tables_failed=sum(1 for t in tables if t.get("status") == "failed"),
        tables_skipped=sum(1 for t in tables if t.get("status") == "skipped"),
        rows_copied=_rows_landed(tables),
        resumed_from=str(data.get("resumed_from") or ""),
        error=_error(data.get("error")),
        tables=[
            LoadTable(
                name=str(t.get("name", "")),
                target=str(t.get("target", "")),
                status=str(t.get("status", "")),
                rows_copied=int(t.get("rows_copied") or 0),
                total_rows=int(t.get("total_rows") or 0),
                error=_error(t.get("error")),
            )
            for t in tables
        ],
    )


def _async_run(kind: str, data: dict) -> LoadRun:
    """An async load, or the provisioning record of a job (``async_job``)."""
    tables: dict = data.get("tables") or {}
    return LoadRun(
        kind=kind,
        run_id=str(data.get("run_id", "")),
        status=str(data.get("status", "")),
        started_at=str(data.get("started_at") or ""),
        finished_at=str(data.get("finished_at") or ""),
        duration=_duration(data.get("started_at"), data.get("finished_at")),
        tables_total=int(data.get("tables_total") or 0) or len(tables),
        tables_ok=sum(1 for t in tables.values() if t.get("status") == "success"),
        tables_failed=sum(1 for t in tables.values() if t.get("status") == "failed"),
        tables_skipped=sum(1 for t in tables.values() if t.get("status") == "skipped"),
        rows_copied=_rows_landed(tables.values()),
        resumed_from=str(data.get("resumed_from") or ""),
        job_url=str(data.get("run_url") or data.get("job_url") or ""),
        scheduled_cron=str(data.get("quartz_cron") or ""),
        error=_error(data.get("error")),
        tables=[
            LoadTable(
                name=name,
                status=str(t.get("status", "")),
                rows_copied=int(t.get("rows_copied") or 0),
                error=_error(t.get("error")),
            )
            for name, t in sorted(tables.items())
        ],
    )


def _result_section(project_id: str) -> ResultSection:
    """Load runs recorded against the project, newest first.

    Fail-soft: an unreachable store yields an empty section, and ``completeness``
    says so rather than the report implying nothing was ever loaded.
    """
    try:
        store = get_run_store()
        persistent = not isinstance(store, MemoryRunStore)
        records = [
            record
            for kind in _LOAD_KINDS
            for record in store.list(kind, _RUN_HORIZON, project_id)
        ]
    except Exception:
        return ResultSection(history_persistent=False)

    records.sort(key=lambda r: r.updated_at, reverse=True)
    runs: list[LoadRun] = []
    for record in records[:_RUN_CAP]:
        try:
            data = store.load(record.kind, record.run_id)
        except Exception:
            continue
        if not data:
            continue
        runs.append(
            _sync_run(data) if record.kind == "sync_run" else _async_run(record.kind, data)
        )

    return ResultSection(
        runs=runs,
        runs_total=len(records),
        **_totals(runs),
        history_persistent=persistent,
    )


def _totals(runs: list[LoadRun]) -> dict[str, int]:
    """Rows in the target from these loads, counted once per table.

    Summing whole runs double-counts in two ways a real project hits: a resume carries
    the skipped tables' counts from the run it continued, and a scheduled snapshot
    re-copies every table on every refresh. Taking each table from the newest load that
    landed it handles both without having to tell them apart. A provisioning record
    (``async_job``) copied nothing, so it contributes nothing.
    """
    seen: dict[str, int] = {}
    for run in runs:            # newest first
        if run.kind == "async_job":
            continue
        for table in run.tables:
            if table.status in _TABLE_DONE and table.name not in seen:
                seen[table.name] = table.rows_copied
    return {"rows_copied": sum(seen.values()), "tables_loaded": len(seen)}


def _validation_section(report: ValidationReport) -> ValidationSection:
    outstanding = [i for i in report.items if i.status is not MatchStatus.MATCHED]
    rows = [
        ValidationRow(
            id=item.id,
            name=item.target_name or item.source_name,
            kind=item.kind.value,
            status=item.status.value,
            severity=item.severity.value,
            detail=item.detail,
            recommendation=item.recommendation,
            source_rows=item.source_rows,
            target_rows=item.target_rows,
            rows_approximate=item.rows_approximate,
            columns_missing=item.columns_missing[:_DIFF_CAP],
            columns_extra=item.columns_extra[:_DIFF_CAP],
            type_drift=item.type_drift[:_DIFF_CAP],
            collation_drift=item.collation_drift[:_DIFF_CAP],
            objects=[
                f"{o.name} ({o.status.value})"
                for o in item.objects
                if o.status is not MatchStatus.MATCHED
            ][:_DIFF_CAP],
        )
        for item in outstanding[:_OUTSTANDING_CAP]
    ]
    return ValidationSection(
        generated_at=report.generated_at,
        match_score=report.match_score,
        total_source=report.total_source,
        matched=report.matched,
        missing=report.missing,
        mismatched=report.mismatched,
        extra=report.extra,
        source_rows=report.source_rows,
        target_rows=report.target_rows,
        row_delta=report.target_rows - report.source_rows,
        tables_compared=report.tables_compared,
        tables_estimated=report.tables_estimated,
        remediated=sum(1 for i in report.items if i.remediated),
        outstanding=rows,
        outstanding_total=len(outstanding),
    )


def _parity_section(report: QueryParityReport) -> ParitySection:
    speedup = (
        report.source_total_ms / report.target_total_ms
        if report.source_total_ms and report.target_total_ms
        else None
    )
    return ParitySection(
        generated_at=report.generated_at,
        endpoint=report.endpoint,
        parity_score=report.parity_score,
        requested=report.requested,
        total=report.total,
        matched=report.matched,
        mismatched=report.mismatched,
        errored=report.errored,
        source_total_ms=report.source_total_ms,
        target_total_ms=report.target_total_ms,
        speedup=round(speedup, 2) if speedup else None,
        queries=[
            ParityRow(
                id=c.query.id,
                title=c.query.title,
                category=c.query.category,
                intent=c.query.intent,
                status=c.status.value,
                count_match=c.count_match,
                format_match=c.format_match,
                source_rows=c.source.row_count,
                target_rows=c.target.row_count,
                source_ms=c.source.duration_ms,
                target_ms=c.target.duration_ms,
                speedup_ratio=c.speedup_ratio,
                detail=c.detail,
                mismatch_columns=c.mismatch_columns[:_DIFF_CAP],
                source_error=_error(c.source.error),
                target_error=_error(c.target.error),
            )
            for c in report.comparisons
        ],
    )


def _headline(
    project: Project,
    report: AssessmentReport | None,
    plan: list[PlanItem],
    result: ResultSection | None,
    validation: ValidationReport | None,
    parity: QueryParityReport | None,
) -> Headline:
    return Headline(
        readiness_score=report.readiness_score if report else None,
        tables=report.table_count if report else 0,
        total_rows=report.total_rows if report else 0,
        programmable_objects=report.programmable_object_count if report else 0,
        tables_selected=len(project.selection),
        plan_items=len(plan),
        rows_copied=result.rows_copied if (result and result.runs) else None,
        match_score=validation.match_score if validation else None,
        parity_score=parity.parity_score if parity else None,
    )


# Assessment row counts come from sys.dm_db_partition_stats, not COUNT(*) — cheap, and
# approximate. A client reading "576,084 rows" would otherwise take it as exact.
_APPROXIMATE_ROWS = (
    "Source row counts come from the source's own partition statistics rather than "
    "counting every row, so they are approximate."
)
_SCAN_AGE = (
    "This describes the source as it was when it was last scanned, not as it is now. "
    "Re-run the assessment if the source has changed since."
)


def _assessment_completeness(report: AssessmentReport | None) -> list[str]:
    """The assessment-only export: the caveats about the scan itself, and no others.

    The migration-wide notes are deliberately absent — an artifact that covers only the
    assessment should not list a plan, a load or a validation as missing from it.
    """
    if report is None:
        return ["No assessment is stored for this project, so there is nothing to report."]
    out = [_SCAN_AGE]
    if report.total_rows:
        out.append(_APPROXIMATE_ROWS)
    if report.ai_assessment and report.ai_assessment.success:
        out.append(
            "The AI migration analysis is one model's reading of the schema and code. It "
            "is advisory, and the readiness score and findings beside it are not — those "
            "come from the deterministic rule checks."
        )
    return out


def _has_errors(result: ResultSection | None, parity: QueryParityReport | None) -> bool:
    """Whether the report carries any database error text at all."""
    runs = result.runs if result else []
    if any(r.error or any(t.error for t in r.tables) for r in runs):
        return True
    return any(c.source.error or c.target.error for c in (parity.comparisons if parity else []))


def _completeness(
    project: Project,
    report: AssessmentReport | None,
    plan: list[PlanItem],
    result: ResultSection,
    validation: ValidationReport | None,
    parity: QueryParityReport | None,
) -> list[str]:
    """What the report cannot vouch for. A report can be exported mid-migration."""
    out: list[str] = []
    if report is None:
        out.append(
            "No assessment is stored for this project, so there is nothing to report "
            "on the source, the plan, or what was compared afterwards."
        )
        return out
    if report.total_rows:
        out.append(_APPROXIMATE_ROWS)
    if not plan:
        out.append(
            "No migration plan was built, so nothing here says what the migration "
            "was going to create in the target."
        )
    else:
        # The apply is synchronous and hands its results to the browser without storing
        # them, so what reached the target is only ever evidenced by Validation.
        out.append(
            "Applying the schema and code plan is not recorded, so this report cannot "
            "state which plan items succeeded. Validation is the evidence for what is "
            "actually in the target."
        )
    if not project.selection and not result.runs:
        out.append(
            "No tables were selected for the data load, so this report covers schema "
            "and object changes only."
        )
    elif not result.runs:
        out.append(
            "No data load is recorded against this project, so no rows are reported "
            "as copied."
        )
    if not result.history_persistent:
        out.append(
            "Run history is not held in a durable store, so any load recorded before "
            "the last restart is missing here and the data-load section can read empty "
            "even though tables were copied."
        )
    if _has_errors(result, parity):
        out.append(
            "Database error messages are shortened to their first line, so a value from "
            "a failing row cannot travel in this report. Open the run or the query in "
            "Lakebase Express for the full text."
        )
    if validation is None:
        out.append(
            "Validation has not been run: object coverage, row counts, structure and "
            "collations in the target are unverified."
        )
    elif validation.match_score < 100:
        out.append(
            f"Validation matched {validation.match_score}% of compared objects — the "
            "rest are listed as outstanding and were not resolved."
        )
    if validation is not None and validation.tables_estimated:
        out.append(
            f"{validation.tables_estimated} of {validation.tables_compared} compared "
            "tables were counted by planner estimate rather than an exact COUNT(*), so "
            "their row totals are approximate."
        )
    if parity is None:
        out.append(
            "Query parity has not been run: no behavioural difference has been proven "
            "by executing queries on both sides."
        )
    elif parity.parity_score < 100:
        out.append(
            f"Query parity scored {parity.parity_score}% — the queries that disagreed "
            "or failed are listed with their differences."
        )
    out.append(
        "Sizing and cost are calculated on demand and not stored on the project, so "
        "they are not part of this report."
    )
    return out


# --- Entry point ------------------------------------------------------------------


def build_report(
    project: Project, *, tool_version: str = "", scope: str = SCOPE_FULL
) -> MigrationReport:
    """Render the report for ``project``.

    ``scope="assessment"`` carries the source scan alone — the deliverable for the
    stage where there is no plan, no load and nothing compared yet. It reads no run
    history, so it costs a single project read.
    """
    report = _assessment(project)
    assessment_only = scope == SCOPE_ASSESSMENT
    plan = [] if assessment_only else _plan(project)
    validation = None if assessment_only else _validation(project)
    parity = None if assessment_only else _parity(project)
    result = None if assessment_only else _result_section(project.id)

    statuses = {
        k: (v.value if hasattr(v, "value") else str(v)) for k, v in project.statuses.items()
    }
    completeness = (
        _assessment_completeness(report) if assessment_only
        else _completeness(project, report, plan, result, validation, parity)
    )
    return MigrationReport(
        scope=scope,
        provenance=ReportProvenance(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            project_id=project.id,
            project_name=project.name,
            tool_version=tool_version,
            phase_statuses=statuses,
            completeness=completeness,
        ),
        coordinates=_coordinates(project),
        headline=_headline(project, report, plan, result, validation, parity),
        assessment=_assessment_section(report) if report else None,
        plan=_plan_section(plan) if plan else None,
        result=result,
        validation=_validation_section(validation) if validation else None,
        parity=_parity_section(parity) if parity else None,
    )
