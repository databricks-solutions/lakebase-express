"""The drop-in SKILL.md — what an agent is handed, and what it must not contain."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import context_routes
from backend.assessment.models import (
    AIAssessment,
    AIRisk,
    CheckConstraintInfo,
    Finding,
    IndexColumnInfo,
    IndexInfo,
    ProgrammableObject,
    SecretRef,
    Severity,
)
from backend.context_bundle.builder import build_bundle
from backend.context_bundle.skill import SKILL_NAME, render_skill
from backend.migration.models import ObjectKind
from backend.projects.models import Project
from backend.projects.store import LocalFileStore
from backend.schema_migration.naming import IdentifierCase
from backend.validation.models import MatchStatus, ValidationItem, ValidationReport
from tests.test_context_bundle import (
    CI_COLLATION,
    _UUID,
    _col,
    _plan_item,
    _project,
    _report,
    _table,
)


def _skill(**kw) -> str:
    return render_skill(build_bundle(_project(**kw), tool_version="0.1.0"))


# --- Shape ------------------------------------------------------------------------


def test_frontmatter_makes_it_loadable_as_a_skill():
    text = _skill(report=_report())
    lines = text.splitlines()

    assert lines[0] == "---"
    assert lines[1] == f"name: {SKILL_NAME}"
    assert lines[2].startswith('description: "')
    assert lines[3] == "---"


def test_sections_appear_in_blast_radius_order():
    text = _skill(report=_report())
    headings = [line for line in text.splitlines() if line.startswith("## ")]

    assert headings == [
        "## 1. Identifiers and quoting",
        "## 2. Columns that change how your code must read or compare values",
        "## 3. Procedure, function, view and trigger call sites",
        "## 4. T-SQL embedded in application code",
        "## 5. What did not come across",
        "## 6. Runtime behaviour",
        '## 7. Do not "fix" these',
    ]


def test_it_tells_the_agent_how_to_read_it():
    text = _skill(report=_report())

    assert "work through the sections in order" in text
    assert "treat silence as \"no change needed\"" in text


# --- Grouping is what keeps it readable -------------------------------------------


def test_columns_are_grouped_by_change_not_listed_per_row():
    tables = [_table(f"T{i}", [_col("Flag", "bit"), _col("Id", "int")]) for i in range(12)]
    text = _skill(report=_report(tables=tables))

    # One heading for the change, with its count — not twelve table rows.
    assert "### `bit` → `boolean` — 12 column(s)" in text
    assert text.count("`bit`") < 12
    # The columns are still named: "which ones" is the actionable part.
    assert "`public.t0.Flag`" in text and "`public.t11.Flag`" in text
    # And the explanation appears once.
    assert text.count("no longer type-check") == 1


def test_a_group_names_every_column_it_covers():
    """Truncating removes exactly what the reader needs — which columns to change —
    and a partial list silently under-reports the work."""
    tables = [_table(f"T{i}", [_col("Flag", "bit")]) for i in range(60)]
    text = _skill(report=_report(tables=tables))

    assert "— 60 column(s)" in text
    for i in range(60):
        assert f"`public.t{i}.Flag`" in text
    assert "more." not in text


def test_a_type_that_keeps_its_name_does_not_read_as_a_no_op():
    table = _table("Model", [_col("Spec", "xml")])
    text = _skill(report=_report(tables=[table]))

    assert "`xml` — same type name, different capabilities" in text
    assert "`xml` → `xml`" not in text


def test_unchanged_columns_are_accounted_for():
    table = _table("Orders", [_col("Flag", "bit")] + [_col(f"c{i}", "int") for i in range(9)])
    text = _skill(report=_report(tables=[table]))

    assert "1 of 10 columns changed observably" in text


# --- The dangerous specifics ------------------------------------------------------


def test_pattern_matching_failure_is_spelled_out_with_its_columns():
    table = _table("Customer", [
        _col("Email", "nvarchar", max_length=100, collation_name=CI_COLLATION),
    ])
    text = _skill(report=_report(tables=[table]))

    assert "`LIKE` and regex now fail" in text
    assert "`public.customer.Email`" in text


def test_preserve_mode_warns_that_unquoted_sql_breaks():
    text = _skill(report=_report(tables=[_table("Product", [_col("Id", "int")])]),
                  identifier_case=IdentifierCase.PRESERVE)

    assert "MUST quote" in text
    assert "folds to `modifieddate` and fails" in text


def test_lowercase_mode_lists_the_renames():
    text = _skill(report=_report(tables=[_table("Product", [_col("Id", "int")], schema="SalesLT")]),
                  identifier_case=IdentifierCase.LOWERCASE)

    assert "| `SalesLT.Product` | `saleslt.product` |" in text


def test_trades_are_present_so_an_agent_does_not_undo_them():
    table = _table("Customer", [
        _col("Name", "nvarchar", max_length=50, collation_name=CI_COLLATION),
    ])
    text = _skill(report=_report(tables=[table]))

    assert 'Do not "fix" these' in text
    assert "Do not switch them to deterministic collations" in text
    assert "substitute citext" in text


def test_unapplied_predicates_are_reported_as_unenforced():
    """A CHECK that failed to apply is one the application can no longer assume is
    enforced — the ISJSON class of failure."""
    table = _table(
        "Profile", [_col("Prefs", "nvarchar", max_length=-1)],
        check_constraints=[CheckConstraintInfo(
            name="CK_Prefs", definition="(isjson([Prefs])=(1))")],
    )
    plan = [_plan_item("check:dbo.Profile.CK_Prefs", ObjectKind.CONSTRAINT,
                       "public.profile · CHECK ck_prefs",
                       'ALTER TABLE "public"."profile" ADD CONSTRAINT "ck_prefs" '
                       'CHECK ((isjson("Prefs")=(1)));')]
    text = _skill(report=_report(tables=[table]), plan=plan)

    assert "Constraints and defaults that may not be enforced" in text
    assert "isjson" in text


def test_call_site_rules_are_stated_once_per_object_type():
    """The rule was repeated verbatim on all 29 rows of a real export."""
    plan = [
        _plan_item(f"procedure:dbo.P{i}", ObjectKind.PROCEDURE, f"public.p{i}", "CREATE ...")
        for i in range(3)
    ]
    text = _skill(report=_report(), plan=plan)

    assert "### Procedures in the target — 3" in text
    assert text.count("OUT parameters become INOUT") == 1
    for i in range(3):
        assert f"`public.p{i}`" in text


def test_a_reshaped_procedure_is_listed_as_a_function_with_its_call_form():
    """A procedure that returns rows is a function in the target. Listing it under
    "Procedures" beside a `CALL` rule is what made a migrated app fail with 42809."""
    plan = [
        _plan_item(
            "procedure:dbo.usp_ItemReport", ObjectKind.PROCEDURE,
            "public.usp_itemreport",
            "CREATE OR REPLACE FUNCTION public.usp_itemreport(p_category text) "
            "RETURNS TABLE(item_id int) AS $$ BEGIN RETURN QUERY SELECT 1; END $$;",
            original="CREATE PROCEDURE dbo.usp_ItemReport AS BEGIN SELECT * FROM dbo.Items; END",
        ),
    ]
    text = _skill(report=_report(), plan=plan)

    assert "### Functions in the target — 1" in text
    assert "### Procedures in the target" not in text
    assert "was a procedure in SQL Server and is a **function** here" in text
    assert "SELECT * FROM public.usp_itemreport(...)" in text


def test_a_row_returning_procedure_left_as_a_procedure_is_flagged_as_undoable():
    plan = [
        _plan_item(
            "procedure:dbo.usp_ItemReport", ObjectKind.PROCEDURE,
            "public.usp_itemreport",
            "CREATE OR REPLACE PROCEDURE public.usp_itemreport(p text) AS $$ BEGIN END $$;",
            original="CREATE PROCEDURE dbo.usp_ItemReport AS BEGIN SELECT * FROM dbo.Items; END",
        ),
    ]
    text = _skill(report=_report(), plan=plan)

    assert "[!CAUTION]" in text
    assert "do not" in text and "work around this in application" in text
    assert "`dbo.usp_ItemReport`" in text
    # The gap must reach the skill too: the renderer drops origins it does not list,
    # so a gap present in the JSON bundle can still be missing from the handover.
    assert "Objects whose target shape cannot serve their caller" in text
    assert "RETURNS TABLE" in text


def test_untranslated_callables_are_called_out():
    plan = [_plan_item("procedure:dbo.Missing", ObjectKind.PROCEDURE, "public.missing", "")]
    text = _skill(report=_report(), plan=plan)

    assert "the application has to provide the logic itself" in text
    assert "`dbo.Missing`" in text


def test_rewrites_separate_what_this_database_proved():
    findings = [Finding(rule_id="TOP", title="TOP clause", severity=Severity.LOW,
                        object_name="dbo.P (PROCEDURE)", detail="", recommendation="")]
    text = _skill(report=_report(findings=findings))

    proved = text.index("Found in this database")
    other = text.index("Not found in the database")
    assert text.index("SELECT TOP n ...") < other      # listed under "found"
    assert proved < other


def test_pipes_in_rewrites_do_not_break_the_table():
    text = _skill(report=_report())

    # `||` would otherwise split the row into extra columns.
    assert "'a' \\|\\| b" in text
    assert "|| b, or concat" not in text


# --- What must not be in it -------------------------------------------------------


def test_stale_ai_advice_is_not_in_the_skill():
    """It recommends citext and timestamp(6) — both contradicted by the trades in
    the same document."""
    ai = AIAssessment(success=True, risks=[AIRisk(
        title="Use citext", severity="high", rationale="r",
        recommendation="Model case-insensitive columns with the citext extension")])
    text = _skill(report=_report(ai_assessment=ai))

    assert "citext extension" not in text
    assert "Model-suggested risks" not in text
    # And the exclusion is not explained to the reader either — it is not their problem.
    assert "AI migration analysis" not in text


# Coordinates that must never reach an artifact meant to travel, and the markers used
# to prove it. Kept as one list so both export formats are checked against the same set.
_INFRASTRUCTURE = (
    "instance.database.cloud.databricks.com", "sqlserver-prod-01.database.windows.net",
    "svc@example.com", "customer-vault", "pg-password", "sqlserver-prod-01", "SalesDB",
    "databricks_postgres", "psycopg",
)


def _loaded_project() -> Project:
    """A project with every section populated and every coordinate set.

    The plan, validation and notes paths matter: most of what the bundle emits is
    derived from them, so a redaction check that only covers a bare project is
    checking the one shape where there is least to leak.
    """
    sql = ('CREATE OR REPLACE FUNCTION public.usp_r() RETURNS TABLE(a int) '
           'LANGUAGE plpgsql AS $$ BEGIN RETURN QUERY SELECT 1; END $$;')
    validation = ValidationReport(
        source_database="SalesDB", target_database="databricks_postgres",
        target_schema="public", match_score=100,
        items=[ValidationItem(id="procedure:dbo.usp_R", kind=ObjectKind.PROCEDURE,
                              source_name="dbo.usp_R", target_name="public.usp_r",
                              target_kind="function", status=MatchStatus.MATCHED,
                              severity=Severity.INFO)],
    ).model_dump(mode="json")
    project = _project(
        name="Prod DB on sqlserver-prod-01", report=_report(database="SalesDB"),
        plan=[_plan_item("procedure:dbo.usp_R", ObjectKind.PROCEDURE, "public.usp_r", sql,
                         original="CREATE PROCEDURE dbo.usp_R AS BEGIN SELECT 1 FROM t; END",
                         reasoning="r", notes="n")],
        validation=validation,
    )
    project.source.host = "sqlserver-prod-01.database.windows.net"
    project.source.username = "svc@example.com"
    project.source.database = "SalesDB"
    project.source.secret_ref = SecretRef(scope="customer-vault", key="pg-password")
    project.target.host = "instance.database.cloud.databricks.com"
    project.target.user = "svc@example.com"
    project.target.database = "databricks_postgres"
    project.target.secret_ref = SecretRef(scope="customer-vault", key="pg-password")
    return project


def test_the_export_never_names_the_infrastructure():
    """It travels to another repo or agent, so a hostname in it is disclosure with
    nothing gained — how the app connects is not this artifact's job."""
    text = render_skill(build_bundle(_loaded_project()))

    for detail in _INFRASTRUCTURE:
        assert detail not in text, detail


def test_the_json_bundle_redacts_the_same_things_as_the_skill():
    """Both formats are served, and only one of them was ever checked."""
    import json

    from backend.context_bundle.models import AiNotes, AiObjectNote

    notes = AiNotes(endpoint="databricks-claude-opus-4-8", success=True, objects_total=1,
                    generated_at="2026-09-28T15:11:40+00:00",
                    notes=[AiObjectNote(source="dbo.usp_R", object_type="PROCEDURE",
                                        call_site="c", behaviour="b", watch_out="w")])
    blob = json.dumps(build_bundle(_loaded_project(), ai_notes=notes).model_dump(mode="json"))

    for detail in _INFRASTRUCTURE:
        assert detail not in blob, detail


def test_incomplete_migrations_warn_at_the_top():
    validation = ValidationReport(
        source_database="s", target_database="t", target_schema="public", match_score=80,
        items=[ValidationItem(id="table:dbo.Gone", kind=ObjectKind.TABLE,
                              target_name="public.gone", status=MatchStatus.MISSING,
                              severity=Severity.HIGH, detail="not in target")],
    )
    text = _skill(report=_report(), validation=validation.model_dump(mode="json"))

    warning = text.index("What this skill cannot vouch for")
    assert warning < text.index("## 1.")
    assert "matched 80%" in text
    assert "Objects that do not match in the target" in text


# --- Scale ------------------------------------------------------------------------


def test_a_wide_database_still_renders_a_readable_skill():
    """Grouping is the only thing keeping this small — per-column rows would be
    hundreds of kilobytes."""
    tables = []
    for t in range(200):
        columns = [_col(f"c{i}", "int") for i in range(18)]
        columns.append(_col("IsPaid", "bit"))
        columns.append(_col("Name", "nvarchar", max_length=50, collation_name=CI_COLLATION))
        tables.append(_table(f"Table{t}", columns, primary_key=["c0"],
                             indexes=[IndexInfo(name="IX_U",
                                                columns=[IndexColumnInfo(name="c0")],
                                                is_unique=True)]))
    objects = [ProgrammableObject(schema_name="dbo", object_name=f"P{i}",
                                  object_type="PROCEDURE", line_count=10, definition="x")
               for i in range(30)]
    text = render_skill(build_bundle(_project(
        report=_report(tables=tables, programmable_objects=objects))))

    assert len(text) < 60_000, f"skill grew to {len(text):,} chars"
    # Every affected column is still named…
    assert "`public.table199.IsPaid`" in text
    # …while the mechanical renames defer to their rule rather than listing 200 rows.
    assert "every one following the same rule" in text


# --- Route ------------------------------------------------------------------------


def _client(monkeypatch, store) -> TestClient:
    monkeypatch.setattr(context_routes, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(context_routes.router)
    return TestClient(app)


def test_route_serves_markdown(tmp_path, monkeypatch):
    store = LocalFileStore(str(tmp_path))
    store.save(_project(report=_report(tables=[_table("Orders", [_col("IsPaid", "bit")])])))
    client = _client(monkeypatch, store)

    response = client.get(f"/api/projects/{_UUID}/context-skill")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("---\nname: lakebase-app-migration")


def test_route_404s_for_an_unknown_project(tmp_path, monkeypatch):
    client = _client(monkeypatch, LocalFileStore(str(tmp_path)))

    assert client.get("/api/projects/nope/context-skill").status_code == 404
