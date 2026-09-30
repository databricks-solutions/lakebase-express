"""Exportable migration report — what it counts, what it admits, and what it escapes."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import report_routes
from backend.assessment.models import (
    AIAssessment,
    AIRisk,
    Finding,
    ProgrammableObject,
    SecretRef,
    Severity,
)
from backend.migration.models import ObjectKind
from backend.projects.models import PhaseStatus
from backend.projects.store import LocalFileStore
from backend.query_parity.models import (
    ParityStatus,
    QueryComparison,
    QueryParityReport,
    RowDiff,
    SideResult,
    SyntheticQuery,
)
from backend.report import builder
from backend.report.builder import build_report
from backend.report.html import render_report
from backend.report.models import SCOPE_ASSESSMENT, MigrationReport
from backend.run_store import MemoryRunStore
from backend.validation.models import (
    MatchStatus,
    ObjectDiff,
    ValidationItem,
    ValidationReport,
)
from tests.test_context_bundle import _UUID, _col, _plan_item, _project, _report, _table


@pytest.fixture
def runs(monkeypatch) -> MemoryRunStore:
    """A run store of its own, so a test's runs never leak into another's report."""
    store = MemoryRunStore()
    monkeypatch.setattr(builder, "get_run_store", lambda: store)
    return store


def _build(**kw) -> MigrationReport:
    return build_report(_project(**kw), tool_version="0.1.0")


def _html(**kw) -> str:
    return render_report(_build(**kw))


def _validation_report(**kw) -> dict:
    return ValidationReport(
        source_database="SalesDB",
        target_database="databricks_postgres",
        target_schema="public",
        **kw,
    ).model_dump(mode="json")


def _sync_run(run_id: str, tables: list[dict], **kw) -> dict:
    return {
        "run_id": run_id,
        "project_id": _UUID,
        "status": kw.pop("status", "success"),
        "started_at": kw.pop("started_at", "2026-01-01T10:00:00+00:00"),
        "finished_at": kw.pop("finished_at", "2026-01-01T10:04:30+00:00"),
        "tables": tables,
        **kw,
    }


def _loaded(name: str, rows: int, status: str = "success", **kw) -> dict:
    return {"name": name, "target": name.lower(), "status": status,
            "rows_copied": rows, "total_rows": rows, **kw}


# --- Sections follow the audit cycle ----------------------------------------------


def test_sections_are_ordered_as_the_migration_happened(runs):
    headings = [line for line in _html(report=_report()).splitlines() if line.startswith("<h2>")]

    assert headings == [
        "<h2>1. Assessment of the source</h2>",
        "<h2>2. Migration plan</h2>",
        "<h2>3. What the migration did</h2>",
        "<h2>4. Validation — source against target</h2>",
        "<h2>5. Query parity — the same question asked of both</h2>",
    ]


def test_a_phase_that_never_ran_is_a_section_saying_so(runs):
    """Every section is rendered either way: a missing heading would let a reader
    conclude the phase passed."""
    html = _html(report=_report())

    assert "No plan was built" in html
    assert "No data load is recorded" in html
    assert "Validation has not been run" in html
    assert "Query parity has not been run" in html


# --- Scores distinguish "zero" from "never measured" ------------------------------


def test_unmeasured_scores_are_none_not_zero(runs):
    report = _build(report=_report())

    assert report.headline.readiness_score == 100
    assert report.headline.match_score is None
    assert report.headline.parity_score is None
    assert report.headline.rows_copied is None


def test_an_unmeasured_score_prints_as_not_run_not_as_a_number(runs):
    html = _html(report=_report())

    assert html.count("Not run") == 2          # validation and parity, not readiness
    assert '<div class="score__n">100/100</div>' in html
    assert "Rows copied" in html and "—" in html


def test_a_genuine_zero_is_reported_as_zero(runs):
    """A migration that scored 0 must not read like one that was never measured."""
    report = _build(report=_report(readiness_score=0),
                    validation=_validation_report(match_score=0))
    html = render_report(report)

    assert (report.headline.readiness_score, report.headline.match_score) == (0, 0)
    assert '<div class="score__n">0/100</div>' in html
    assert '<div class="score__n">0%</div>' in html
    assert html.count("Not run") == 1          # parity only


# --- Assessment -------------------------------------------------------------------


def test_findings_are_grouped_by_rule_with_every_object_counted(runs):
    findings = [
        Finding(rule_id="CURSOR", title="Cursor needs a rewrite", severity=Severity.HIGH,
                object_name=f"dbo.p{i}", detail="d", recommendation="r")
        for i in range(40)
    ]
    section = _build(report=_report(findings=findings)).assessment

    assert len(section.findings) == 1
    group = section.findings[0]
    assert group.affected_total == 40
    assert len(group.affected) == builder._AFFECTED_CAP
    assert section.findings_total == 40


def test_findings_lead_with_the_highest_severity(runs):
    findings = [
        Finding(rule_id="LOW_ONE", title="low", severity=Severity.LOW,
                object_name="dbo.a", detail="", recommendation=""),
        Finding(rule_id="HIGH_ONE", title="high", severity=Severity.HIGH,
                object_name="dbo.b", detail="", recommendation=""),
        Finding(rule_id="MED_ONE", title="medium", severity=Severity.MEDIUM,
                object_name="dbo.c", detail="", recommendation=""),
    ]
    section = _build(report=_report(findings=findings)).assessment

    assert [f.severity for f in section.findings] == ["high", "medium", "low"]


def test_the_readiness_score_ships_with_its_formula(runs):
    """A penalty sum clamped at 0 reads as total failure without it."""
    html = _html(report=_report(readiness_score=0))

    assert "clamped to 0-100" in html


def test_largest_tables_are_capped_and_say_how_many_were_left_out(runs):
    tables = [_table(f"T{i}", [_col("Id", "int")], row_count=i) for i in range(40)]
    report = _build(report=_report(tables=tables))

    assert len(report.assessment.largest_tables) == builder._TABLE_CAP
    assert report.assessment.tables_total == 40
    assert report.assessment.largest_tables[0].rows == 39     # ordered by size
    assert "…and 25 more tables" in render_report(report)


def test_a_capped_list_points_at_the_project_not_at_the_json_export(runs):
    """The caps are applied when the report is built, so the JSON carries the same
    ones — promising the rest is there would send a reader nowhere."""
    tables = [_table(f"T{i}", [_col("Id", "int")], row_count=i) for i in range(40)]
    html = _html(report=_report(tables=tables))

    assert "the project's Assessment holds all of them" in html
    assert "JSON export" not in html


def test_the_ai_analysis_is_carried_but_labelled_as_written_before_the_plan(runs):
    """Unlike the app-migration skill, the report is a record of what each phase
    produced — so the analysis belongs, with what dates it."""
    ai = AIAssessment(
        summary="A summary.", complexity="High", endpoint="my-endpoint", success=True,
        risks=[AIRisk(title="A risk", severity="high", rationale="why")],
    )
    html = _html(report=_report(ai_assessment=ai))

    assert "my-endpoint" in html
    assert "before the migration plan existed" in html
    assert "A risk" in html


def test_a_failed_ai_analysis_is_left_out_rather_than_rendered_empty(runs):
    ai = AIAssessment(success=False, error="endpoint timed out", endpoint="my-endpoint")
    report = _build(report=_report(ai_assessment=ai))

    assert report.assessment.ai is None
    assert "endpoint timed out" not in render_report(report)


# --- Plan -------------------------------------------------------------------------


def _plan_with_code() -> list[dict]:
    return [
        _plan_item("schema:dbo", ObjectKind.SCHEMA, "public", "CREATE SCHEMA public"),
        _plan_item("table:dbo.Orders", ObjectKind.TABLE, "public.orders", "CREATE TABLE ..."),
        _plan_item("index:dbo.Orders.IX", ObjectKind.INDEX, "orders_ix", "CREATE INDEX ..."),
        _plan_item("procedure:dbo.GetRows", ObjectKind.PROCEDURE, "public.getrows",
                   "CREATE FUNCTION public.getrows() RETURNS TABLE(a int) AS $$ $$",
                   reasoning="had to become a function"),
        _plan_item("procedure:dbo.Edited", ObjectKind.PROCEDURE, "public.edited",
                   "CREATE PROCEDURE public.edited() AS $$ $$"),
        _plan_item("procedure:dbo.Untranslated", ObjectKind.PROCEDURE, "public.untranslated", ""),
    ]


def test_the_plan_splits_the_phases_the_apply_actually_uses(runs):
    section = _build(plan=_plan_with_code()).plan

    assert section.total == 6
    assert section.post_data == 1           # the index
    assert section.pre_data == 5
    assert section.by_kind["procedure"] == 3


def test_each_code_object_records_who_translated_it(runs):
    section = _build(plan=_plan_with_code()).plan

    assert {r.source: r.provenance for r in section.code_objects} == {
        "dbo.GetRows": "ai",
        "dbo.Edited": "user-edited",
        "dbo.Untranslated": "not-translated",
    }
    assert (section.translated, section.user_edited, section.not_translated) == (1, 1, 1)


def test_a_reshaped_procedure_reports_what_it_actually_became(runs):
    """The call form follows the created kind, so the report must not restate the
    source kind for a procedure that had to become a function."""
    section = _build(plan=_plan_with_code()).plan
    row = next(r for r in section.code_objects if r.source == "dbo.GetRows")

    assert (row.object_type, row.target_kind) == ("PROCEDURE", "function")


def test_mirrored_collations_are_named(runs):
    plan = [_plan_item("collation:SQL_Latin1_General_CP1_CI_AS", ObjectKind.COLLATION,
                       "public.sql_latin1_general_cp1_ci_as", "CREATE COLLATION ...")]
    report = _build(plan=plan)

    assert report.plan.collations == ["public.sql_latin1_general_cp1_ci_as"]
    assert "public.sql_latin1_general_cp1_ci_as" in render_report(report)


# --- Result -----------------------------------------------------------------------


def test_rows_are_read_from_the_recorded_run(runs):
    runs.save("sync_run", _UUID, "success",
              _sync_run(_UUID, [_loaded("dbo.Orders", 500), _loaded("dbo.Items", 250)]), _UUID)
    report = _build()

    assert report.result.rows_copied == 750
    assert report.result.tables_loaded == 2
    assert report.headline.rows_copied == 750
    assert report.result.runs[0].duration == "4m 30s"


def test_a_failed_tables_rows_are_not_counted_as_copied(runs):
    """Each table is copied in one transaction, so a failure committed nothing — its
    progress-before-failure is not in the target."""
    runs.save("sync_run", _UUID, "partial", _sync_run(
        _UUID,
        [_loaded("dbo.Orders", 500), _loaded("dbo.Items", 90, "failed", error="boom")],
        status="partial",
    ), _UUID)
    report = _build()

    assert report.result.rows_copied == 500
    assert report.result.tables_loaded == 1
    assert report.result.runs[0].tables_failed == 1


def test_a_resumed_runs_rows_are_counted_once(runs):
    """A resume carries the skipped tables' counts, so summing both double-counts."""
    first = "22222222-2222-4222-8222-222222222222"
    runs.save("sync_run", first, "partial",
              _sync_run(first, [_loaded("dbo.Orders", 500)], status="partial"), _UUID)
    runs.save("sync_run", _UUID, "success", _sync_run(
        _UUID,
        [_loaded("dbo.Orders", 500, "skipped"), _loaded("dbo.Items", 250)],
        resumed_from=first,
    ), _UUID)
    report = _build()

    assert report.result.rows_copied == 750
    assert report.result.tables_loaded == 2


def test_a_refresh_that_recopied_every_table_is_not_counted_twice(runs):
    """A scheduled snapshot re-copies everything on every run, so summing whole runs
    would grow the reported total without a row being added to the target."""
    older = "44444444-4444-4444-8444-444444444444"
    runs.save("sync_run", older, "success",
              _sync_run(older, [_loaded("dbo.Orders", 500), _loaded("dbo.Items", 250)],
                        started_at="2026-01-01T09:00:00+00:00",
                        finished_at="2026-01-01T09:03:00+00:00"), _UUID)
    runs.save("sync_run", _UUID, "success",
              _sync_run(_UUID, [_loaded("dbo.Orders", 520), _loaded("dbo.Items", 260)]), _UUID)
    report = _build()

    assert len(report.result.runs) == 2
    assert report.result.rows_copied == 780        # the newest load's counts, once
    assert report.result.tables_loaded == 2


def test_a_table_only_the_older_load_landed_is_still_counted(runs):
    """Counting per table, not per run: the newest load wins a table, it does not
    discard one it never touched."""
    older = "55555555-5555-4555-8555-555555555555"
    runs.save("sync_run", older, "success",
              _sync_run(older, [_loaded("dbo.Archive", 90)],
                        started_at="2026-01-01T09:00:00+00:00",
                        finished_at="2026-01-01T09:01:00+00:00"), _UUID)
    runs.save("sync_run", _UUID, "success",
              _sync_run(_UUID, [_loaded("dbo.Orders", 500)]), _UUID)
    report = _build()

    assert report.result.rows_copied == 590
    assert report.result.tables_loaded == 2


def test_a_provisioned_job_is_recorded_without_claiming_to_have_copied_anything(runs):
    runs.save("async_job", _UUID, "scheduled", {
        "run_id": _UUID, "status": "scheduled", "tables_total": 4, "scheduled": True,
        "quartz_cron": "0 0 3 * * ?", "job_url": "https://example.databricks.com/jobs/1",
    }, _UUID)
    report = _build()
    html = render_report(report)

    assert report.result.rows_copied == 0
    assert report.headline.rows_copied == 0
    assert "Databricks job provisioned" in html
    assert "0 0 3 * * ?" in html


def test_a_project_with_more_runs_than_are_shown_says_so(runs):
    """Records are listed past the render cap — listing carries no payload — so a
    trimmed table can state how many runs it left out."""
    for i in range(builder._RUN_CAP + 4):
        run_id = f"{i:08d}-2222-4222-8222-222222222222"
        runs.save("sync_run", run_id, "success",
                  _sync_run(run_id, [_loaded("dbo.Orders", 10)]), _UUID)
    report = _build()

    assert len(report.result.runs) == builder._RUN_CAP
    assert report.result.runs_total == builder._RUN_CAP + 4
    assert "…and 4 more recorded runs" in render_report(report)


def test_runs_of_another_project_are_not_reported(runs):
    other = "33333333-3333-4333-8333-333333333333"
    runs.save("sync_run", other, "success", _sync_run(other, [_loaded("dbo.Orders", 999)]), other)
    report = _build()

    assert report.result.runs == []
    assert report.headline.rows_copied is None


def test_an_unreachable_run_store_does_not_break_the_export(monkeypatch):
    def explode():
        raise RuntimeError("store is down")

    monkeypatch.setattr(builder, "get_run_store", explode)
    report = _build(report=_report(), selection=["dbo.Orders"])

    assert report.result.runs == []
    assert report.result.history_persistent is False
    assert "not held in a durable store" in " ".join(report.provenance.completeness)


# --- Validation and parity --------------------------------------------------------


def test_only_the_objects_that_did_not_match_are_listed(runs):
    items = [
        ValidationItem(id="table:dbo.Orders", kind=ObjectKind.TABLE, target_name="public.orders",
                       status=MatchStatus.MATCHED),
        ValidationItem(id="procedure:dbo.P", kind=ObjectKind.PROCEDURE, target_name="public.p",
                       status=MatchStatus.MISSING, severity=Severity.HIGH,
                       detail="not in the target", recommendation="translate it by hand"),
    ]
    section = _build(validation=_validation_report(
        match_score=50, total_source=2, matched=1, missing=1, items=items,
    )).validation

    assert [r.id for r in section.outstanding] == ["procedure:dbo.P"]
    assert section.outstanding_total == 1


def test_the_row_delta_is_stated_so_nobody_subtracts(runs):
    section = _build(validation=_validation_report(
        source_rows=1_000, target_rows=940, tables_compared=3,
    )).validation

    assert section.row_delta == -60
    assert "-60" in render_report(_build(validation=_validation_report(
        source_rows=1_000, target_rows=940, tables_compared=3,
    )))


def test_an_estimated_row_count_is_admitted_as_approximate(runs):
    report = _build(report=_report(), validation=_validation_report(
        match_score=100, tables_compared=4, tables_estimated=2,
    ))

    assert "counted by planner estimate" in " ".join(report.provenance.completeness)


def test_a_mismatched_object_carries_the_drift_that_proves_it(runs):
    items = [ValidationItem(
        id="table:dbo.Customer", kind=ObjectKind.TABLE, target_name="public.customer",
        status=MatchStatus.MISMATCH, severity=Severity.MEDIUM,
        source_rows=10, target_rows=10,
        collation_drift=["email: expected ci_as, found default"],
        objects=[ObjectDiff(name="customer_ix", status=MatchStatus.MISSING)],
    )]
    html = render_report(_build(validation=_validation_report(
        match_score=90, total_source=1, mismatched=1, items=items,
    )))

    assert "email: expected ci_as, found default" in html
    assert "customer_ix (missing)" in html


def _parity_report(**kw) -> dict:
    return QueryParityReport(source_database="SalesDB", **kw).model_dump(mode="json")


def _comparison(qid: str, status: ParityStatus, **kw) -> QueryComparison:
    source_error = kw.pop("source_error", None)
    target_error = kw.pop("target_error", None)
    return QueryComparison(
        query=SyntheticQuery(id=qid, title=kw.pop("title", qid), source_sql="SELECT 1",
                             target_sql="SELECT 1"),
        source=SideResult(ok=not source_error, row_count=kw.pop("source_rows", 5),
                          duration_ms=kw.pop("source_ms", 100), error=source_error),
        target=SideResult(ok=not target_error, row_count=kw.pop("target_rows", 5),
                          duration_ms=kw.pop("target_ms", 50), error=target_error),
        status=status,
        **kw,
    )


def test_every_compared_query_pair_is_listed(runs):
    comparisons = [
        _comparison("q1", ParityStatus.MATCH, count_match=True, format_match=True),
        _comparison("q2", ParityStatus.MISMATCH, target_rows=3,
                    mismatch_columns=["email"], detail="fewer rows"),
    ]
    section = _build(query_parity=_parity_report(
        total=2, matched=1, mismatched=1, parity_score=50, comparisons=comparisons,
    )).parity

    assert [q.id for q in section.queries] == ["q1", "q2"]
    assert section.queries[1].mismatch_columns == ["email"]


def test_the_timing_verdict_states_which_side_was_faster(runs):
    html = render_report(_build(query_parity=_parity_report(
        total=1, matched=1, source_total_ms=2_000, target_total_ms=1_000,
        comparisons=[_comparison("q1", ParityStatus.MATCH, count_match=True, format_match=True)],
    )))

    assert "2.0× faster on Lakebase" in html
    assert "neither was warmed" in html      # the comparison is not a benchmark


def test_a_slower_target_is_not_dressed_up_as_a_speedup(runs):
    section = _build(query_parity=_parity_report(
        total=1, source_total_ms=1_000, target_total_ms=4_000,
        comparisons=[_comparison("q1", ParityStatus.MATCH)],
    )).parity

    assert section.speedup == 0.25
    assert "4.0× slower on Lakebase" in render_report(_build(query_parity=_parity_report(
        total=1, source_total_ms=1_000, target_total_ms=4_000,
        comparisons=[_comparison("q1", ParityStatus.MATCH)],
    )))


# --- What it admits ---------------------------------------------------------------


def test_a_report_with_no_assessment_says_only_that(runs):
    report = _build()

    assert report.assessment is None
    assert report.provenance.completeness == [
        "No assessment is stored for this project, so there is nothing to report on the "
        "source, the plan, or what was compared afterwards."
    ]


def test_the_unrecorded_schema_apply_is_admitted_once_a_plan_exists(runs):
    """The apply is synchronous and stores nothing, so the report must not imply it
    knows which items reached the target."""
    without = " ".join(_build(report=_report()).provenance.completeness)
    with_plan = " ".join(_build(report=_report(), plan=_plan_with_code()).provenance.completeness)

    assert "Applying the schema and code plan is not recorded" not in without
    assert "Applying the schema and code plan is not recorded" in with_plan


def test_the_caveats_are_printed_where_a_reader_starts(runs):
    html = _html(report=_report())
    caveats = html.index("What this report cannot vouch for")

    assert caveats < html.index("<h2>1. Assessment of the source</h2>")


def test_sizing_is_named_as_absent_rather_than_silently_missing(runs):
    report = _build(report=_report())

    assert "Sizing and cost" in " ".join(report.provenance.completeness)


# --- The artifact itself ----------------------------------------------------------


def test_the_html_is_self_contained(runs):
    """It travels to a client's inbox and is opened offline, so nothing may be fetched."""
    html = _html(report=_report())

    assert "<style>" in html
    for fetched in ("<link", "src=", "@import", "http://", "https://"):
        assert fetched not in html


def test_it_carries_a_print_stylesheet_so_the_pdf_is_one_keystroke_away(runs):
    html = _html(report=_report())

    assert "@media print" in html
    assert "@page" in html
    assert "window.print()" in html
    assert "break-inside: avoid" in html


def test_sql_in_a_finding_cannot_break_out_of_the_page(runs):
    findings = [Finding(
        rule_id="INJECT", title="<script>alert(1)</script>", severity=Severity.HIGH,
        object_name="dbo.x", detail="a > b && c < d", recommendation='use "quotes"',
    )]
    html = _html(report=_report(findings=findings))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "a &gt; b &amp;&amp; c &lt; d" in html


def test_no_credential_reaches_the_report(runs):
    """Coordinates are the audit trail; the credential behind them never is — not the
    password, and not the secret scope/key it is resolved from."""
    project = _project(report=_report())
    project.source.username = "svc_migrator@corp.example"
    project.target.user = "lakebase_role_owner"
    project.source.secret_ref = SecretRef(
        scope="prod-secrets", key="sql-admin-password",
        workspace_host="https://adb-123.azuredatabricks.net",
    )
    project.target.secret_ref = SecretRef(scope="lakebase-scope", key="pg-role-password")
    dumped = build_report(project).model_dump_json()
    html = render_report(build_report(project))

    # The values, in both artifacts. Hosts and database names are the audit trail and do
    # travel; the identities that connect to them do not.
    for secret in ("prod-secrets", "sql-admin-password", "lakebase-scope",
                   "pg-role-password", "adb-123",
                   "svc_migrator@corp.example", "lakebase_role_owner"):
        assert secret not in dumped
        assert secret not in html
    # The fields, in the model. The HTML says "carries no password" in prose, so the
    # word itself is expected there.
    for field in ("secret_ref", "password"):
        assert field not in dumped


def test_coordinates_record_what_moved_where(runs):
    report = _build(report=_report())

    assert report.coordinates.target_schema == "public"
    assert report.coordinates.identifier_case == "lowercase"


def test_provenance_names_the_tool_and_the_project(runs):
    report = _build(report=_report())

    assert report.provenance.project_id == _UUID
    assert report.provenance.tool_version == "0.1.0"
    assert report.provenance.report_version == "1"


def test_phase_statuses_are_carried_so_a_mid_migration_export_reads_as_one(runs):
    project = _project(report=_report())
    project.statuses["validation"] = PhaseStatus.IN_PROGRESS
    report = build_report(project, tool_version="0.1.0")

    assert report.provenance.phase_statuses["validation"] == "in_progress"
    assert "validation in progress" in render_report(report)


def test_a_report_from_an_older_stored_shape_still_renders(runs):
    """A project saved before a model changed must not make the export raise."""
    project = _project()
    project.assessment = {"unexpected": "shape"}
    project.plan = [{"also": "unexpected"}]
    project.validation = {"nope": True}
    project.query_parity = {"nope": True}
    report = build_report(project)

    assert report.assessment is None
    assert render_report(report)


# --- Database errors must not carry row data ---------------------------------------
#
# libpq appends DETAIL/HINT to a constraint violation, naming the offending key values
# or printing the whole failing row. The app may show that; a report built to be emailed
# to a client must not.

_UNIQUE_VIOLATION = (
    'duplicate key value violates unique constraint "customer_email_key"\n'
    "DETAIL:  Key (email)=(alice@example.com) already exists."
)
_CHECK_VIOLATION = (
    'new row for relation "customer" violates check constraint "chk_age"\n'
    "DETAIL:  Failing row contains (1, Alice Smith, alice@example.com, 1990-01-01, "
    "+44 7700 900123)."
)


def test_a_failing_rows_values_do_not_reach_the_report(runs):
    runs.save("sync_run", _UUID, "partial", _sync_run(
        _UUID,
        [_loaded("dbo.Customer", 0, "failed", error=_CHECK_VIOLATION),
         _loaded("dbo.Orders", 40, "failed", error=_UNIQUE_VIOLATION)],
        status="partial",
    ), _UUID)
    report = _build(report=_report())
    html = render_report(report)
    dumped = report.model_dump_json()

    for value in ("alice@example.com", "Alice Smith", "1990-01-01", "900123",
                  "Failing row contains", "DETAIL"):
        assert value not in dumped
        assert value not in html


def test_the_diagnosis_itself_survives(runs):
    """Truncation is to the primary message, not to nothing — the reason a table failed
    is the point of the section."""
    runs.save("sync_run", _UUID, "partial", _sync_run(
        _UUID, [_loaded("dbo.Customer", 0, "failed", error=_CHECK_VIOLATION)],
        status="partial",
    ), _UUID)
    html = render_report(_build(report=_report()))

    assert "violates check constraint" in html
    assert "chk_age" in html


def test_a_run_level_error_is_shortened_too(runs):
    runs.save("sync_run", _UUID, "failed", _sync_run(
        _UUID, [_loaded("dbo.Orders", 0, "failed")], status="failed",
        error=_UNIQUE_VIOLATION,
    ), _UUID)
    report = _build(report=_report())

    assert report.result.runs[0].error == (
        'duplicate key value violates unique constraint "customer_email_key"'
    )


def test_a_query_error_is_shortened_on_both_sides(runs):
    comparison = _comparison("q1", ParityStatus.ERROR, source_error=_UNIQUE_VIOLATION,
                             target_error=_CHECK_VIOLATION)
    section = _build(report=_report(), query_parity=_parity_report(
        total=1, errored=1, parity_score=0, comparisons=[comparison],
    )).parity

    assert "DETAIL" not in section.queries[0].source_error
    assert "Alice Smith" not in section.queries[0].target_error
    assert "violates check constraint" in section.queries[0].target_error


def test_a_very_long_single_line_error_is_capped(runs):
    runs.save("sync_run", _UUID, "failed", _sync_run(
        _UUID, [_loaded("dbo.Orders", 0, "failed", error="x" * 5_000)], status="failed",
    ), _UUID)
    report = _build(report=_report())

    assert len(report.result.runs[0].tables[0].error) <= builder._ERROR_CAP + 1


def test_the_report_says_its_errors_are_abbreviated(runs):
    """A shortened error read as the whole error is its own kind of wrong."""
    runs.save("sync_run", _UUID, "partial", _sync_run(
        _UUID, [_loaded("dbo.Orders", 0, "failed", error=_UNIQUE_VIOLATION)],
        status="partial",
    ), _UUID)
    notes = " ".join(_build(report=_report()).provenance.completeness)

    assert "shortened to their first line" in notes


def test_a_clean_migration_is_not_told_about_error_shortening(runs):
    runs.save("sync_run", _UUID, "success",
              _sync_run(_UUID, [_loaded("dbo.Orders", 40)]), _UUID)
    notes = " ".join(_build(report=_report()).provenance.completeness)

    assert "shortened" not in notes


def test_result_rows_from_either_database_are_never_carried(runs):
    """Query parity samples real rows from both sides to compare them. Those samples are
    the single most sensitive thing the project row holds, and no export reads them."""
    comparison = QueryComparison(
        query=SyntheticQuery(id="q1", title="Customers", source_sql="SELECT 1",
                             target_sql="SELECT 1"),
        source=SideResult(ok=True, row_count=2, column_names=["email"],
                          preview_rows=[["alice@example.com"], ["bob@example.com"]]),
        target=SideResult(ok=True, row_count=2, column_names=["email"],
                          preview_rows=[["ALICE@example.com"], ["bob@example.com"]]),
        status=ParityStatus.MISMATCH,
        mismatch_columns=["email"],
        row_diffs=[RowDiff(row_index=0, kind="value", source_cells=["alice@example.com"],
                           target_cells=["ALICE@example.com"], diff_columns=["email"])],
    )
    report = _build(report=_report(), query_parity=_parity_report(
        total=1, mismatched=1, parity_score=0, comparisons=[comparison],
    ))
    html = render_report(report)
    dumped = report.model_dump_json()

    for value in ("alice@example.com", "bob@example.com", "ALICE@example.com"):
        assert value not in dumped
        assert value not in html
    # The column that disagreed is named; the values in it are not.
    assert "email" in html


def test_no_sql_body_is_carried(runs):
    """Plan SQL and source definitions are read to classify objects, never emitted: a
    stored procedure body can hold embedded literals and business logic."""
    plan = [_plan_item(
        "procedure:dbo.GetRows", ObjectKind.PROCEDURE, "public.getrows",
        "CREATE FUNCTION public.getrows() RETURNS TABLE(a int) AS $$ "
        "SELECT * FROM t WHERE token = 'sk-live-do-not-leak' $$ LANGUAGE sql",
        original="CREATE PROCEDURE dbo.GetRows AS SELECT * FROM t WHERE token = 'secret'",
        reasoning="translated",
    )]
    objects = [ProgrammableObject(
        schema_name="dbo", object_name="GetRows", object_type="PROCEDURE", line_count=3,
        definition="SELECT * FROM t WHERE token = 'another-secret'",
    )]
    report = _build(report=_report(programmable_objects=objects), plan=plan)
    dumped = report.model_dump_json()
    html = render_report(report)

    for body in ("sk-live-do-not-leak", "another-secret", "WHERE token"):
        assert body not in dumped
        assert body not in html
    # The object is still reported, by name and by what it became.
    assert "public.getrows" in html


# --- The assessment-only scope ----------------------------------------------------


def _assessment_only(**kw) -> MigrationReport:
    return build_report(_project(**kw), tool_version="0.1.0", scope=SCOPE_ASSESSMENT)


def _full_project(**kw) -> dict:
    """A project with every phase done, so scoping has something to leave out."""
    return {
        "report": _report(tables=[_table("Orders", [_col("Id", "int")])]),
        "plan": _plan_with_code(),
        "selection": ["dbo.Orders"],
        "validation": _validation_report(match_score=90, total_source=1, mismatched=1),
        "query_parity": _parity_report(
            total=1, matched=1, comparisons=[_comparison("q1", ParityStatus.MATCH)],
        ),
        **kw,
    }


def test_the_assessment_scope_carries_the_scan_and_nothing_else(runs):
    report = _assessment_only(**_full_project())

    assert report.scope == SCOPE_ASSESSMENT
    assert report.assessment is not None
    assert (report.plan, report.result, report.validation, report.parity) == (None, None, None, None)


def test_an_absent_section_is_none_not_an_empty_one(runs):
    """An empty ResultSection would read as "nothing was loaded" rather than "not part
    of this artifact"."""
    report = _assessment_only(**_full_project())

    assert report.result is None
    assert report.headline.rows_copied is None


def test_the_assessment_scope_reads_no_run_history(runs, monkeypatch):
    """It is the deliverable for the stage before anything has run, so it should cost
    one project read — not a run-store query per load kind."""
    def explode():
        raise AssertionError("the assessment scope must not touch the run store")

    monkeypatch.setattr(builder, "get_run_store", explode)

    assert _assessment_only(report=_report()).assessment is not None


def test_the_assessment_export_is_titled_as_one(runs):
    html = render_report(_assessment_only(**_full_project()))

    assert "<title>Assessment report — Sales migration</title>" in html
    assert "Lakebase Express · Assessment report" in html


def test_the_assessment_export_renders_only_its_own_section(runs):
    html = render_report(_assessment_only(**_full_project()))
    headings = [line for line in html.splitlines() if line.startswith("<h2>")]

    # Unnumbered: "1." implies a sequence, and there is only one section.
    assert headings == ["<h2>Assessment of the source</h2>"]
    for absent in ("Migration plan", "What the migration did", "Validation —", "Query parity"):
        assert absent not in html


def test_the_full_export_keeps_its_numbered_sequence(runs):
    """Numbering is derived from what is included, so the full report is unchanged."""
    headings = [
        line for line in render_report(_build(**_full_project())).splitlines()
        if line.startswith("<h2>")
    ]

    assert headings[0] == "<h2>1. Assessment of the source</h2>"
    assert headings[-1] == "<h2>5. Query parity — the same question asked of both</h2>"


def test_the_assessment_export_does_not_score_phases_it_excludes(runs):
    """A "Validation match — Not run" tile on an assessment report is answering a
    question the artifact never claimed to ask."""
    html = render_report(_assessment_only(**_full_project()))

    assert "Readiness" in html
    assert "Findings" in html
    for absent in ("Validation match", "Query parity", "Plan items", "Rows copied"):
        assert absent not in html


def test_the_assessment_caveats_are_about_the_scan_not_the_migration(runs):
    notes = " ".join(_assessment_only(**_full_project()).provenance.completeness)

    assert "as it was when it was last scanned" in notes
    for absent in ("Validation has not been run", "Query parity has not been run",
                   "No migration plan", "Sizing and cost", "data load"):
        assert absent not in notes


def test_approximate_source_row_counts_are_admitted_in_both_scopes(runs):
    """They come from the source's partition statistics, not COUNT(*) — a client reading
    an exact-looking total would be misled."""
    tables = [_table("Orders", [_col("Id", "int")], row_count=500)]
    scoped = " ".join(_assessment_only(report=_report(tables=tables)).provenance.completeness)
    full = " ".join(_build(report=_report(tables=tables)).provenance.completeness)

    assert "approximate" in scoped
    assert "approximate" in full


def test_a_source_with_no_rows_claims_nothing_about_row_counts(runs):
    notes = " ".join(_assessment_only(report=_report()).provenance.completeness)

    assert "partition statistics" not in notes


def test_the_assessment_export_points_at_the_fuller_report(runs):
    html = render_report(_assessment_only(**_full_project()))

    assert "Covers the source assessment only" in html
    assert "Phase status at export" not in html


# --- Routes -----------------------------------------------------------------------


def _client(tmp_path, monkeypatch, project=None) -> TestClient:
    store = LocalFileStore(str(tmp_path))
    if project is not None:
        store.save(project)
    monkeypatch.setattr(report_routes, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(report_routes.router)
    return TestClient(app)


def test_the_html_route_serves_a_printable_page(tmp_path, monkeypatch, runs):
    client = _client(tmp_path, monkeypatch, _project(report=_report()))

    response = client.get(f"/api/projects/{_UUID}/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Migration report" in response.text


def test_the_json_route_serves_the_same_report(tmp_path, monkeypatch, runs):
    project = _project(report=_report(tables=[_table("Orders", [_col("Id", "int")])]))
    client = _client(tmp_path, monkeypatch, project)

    body = client.get(f"/api/projects/{_UUID}/report-data").json()
    assert body["headline"]["tables"] == 1
    assert body["provenance"]["tool_version"]


def test_an_unknown_project_is_a_404_on_both_routes(tmp_path, monkeypatch, runs):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/api/projects/does-not-exist/report").status_code == 404
    assert client.get("/api/projects/does-not-exist/report-data").status_code == 404


def test_the_scope_param_narrows_either_route(tmp_path, monkeypatch, runs):
    client = _client(tmp_path, monkeypatch, _project(**_full_project()))

    body = client.get(f"/api/projects/{_UUID}/report-data?scope=assessment").json()
    assert body["scope"] == "assessment"
    assert body["assessment"] and body["validation"] is None

    html = client.get(f"/api/projects/{_UUID}/report?scope=assessment").text
    assert "Assessment report" in html
    assert "Query parity" not in html


def test_the_scope_defaults_to_the_whole_cycle(tmp_path, monkeypatch, runs):
    client = _client(tmp_path, monkeypatch, _project(**_full_project()))

    body = client.get(f"/api/projects/{_UUID}/report-data").json()
    assert body["scope"] == "full"
    assert body["validation"] is not None


def test_a_misspelled_scope_is_rejected_rather_than_silently_exporting_everything(
    tmp_path, monkeypatch, runs
):
    client = _client(tmp_path, monkeypatch, _project(**_full_project()))

    assert client.get(f"/api/projects/{_UUID}/report?scope=assesment").status_code == 422
