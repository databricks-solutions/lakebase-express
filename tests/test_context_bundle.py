"""App-migration context bundle — the delta, its joins, and its redaction."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import context_routes
from backend.assessment.models import (
    AIAssessment,
    AIRisk,
    AssessmentReport,
    CheckConstraintInfo,
    ColumnDefaultInfo,
    ColumnInfo,
    Finding,
    IndexColumnInfo,
    IndexInfo,
    ProgrammableObject,
    SecretRef,
    Severity,
    TableInfo,
)
from backend.context_bundle import rules
from backend.context_bundle.builder import build_bundle
from backend.migration.models import ObjectKind
from backend.projects.models import Project
from backend.projects.store import LocalFileStore
from backend.schema_migration.naming import IdentifierCase
from backend.validation.models import MatchStatus, ValidationItem, ValidationReport

_UUID = "11111111-1111-4111-8111-111111111111"

CI_COLLATION = "SQL_Latin1_General_CP1_CI_AS"    # case-insensitive -> nondeterministic
CS_COLLATION = "SQL_Latin1_General_CP1_CS_AS"    # case-sensitive -> deterministic


def _col(name: str, data_type: str, **kw) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, **kw)


def _table(name: str, columns: list[ColumnInfo], *, schema: str = "dbo", **kw) -> TableInfo:
    return TableInfo(
        schema_name=schema,
        table_name=name,
        row_count=kw.pop("row_count", 10),
        column_count=len(columns),
        columns=columns,
        **kw,
    )


def _report(**kw) -> AssessmentReport:
    tables = kw.pop("tables", [])
    objects = kw.pop("programmable_objects", [])
    findings = kw.pop("findings", [])
    return AssessmentReport(
        database=kw.pop("database", "SalesDB"),
        table_count=len(tables),
        total_rows=sum(t.row_count for t in tables),
        programmable_object_count=len(objects),
        findings=findings,
        readiness_score=kw.pop("readiness_score", 100),
        severity_counts=kw.pop("severity_counts", {"info": 0, "low": 0, "medium": 0, "high": 0}),
        tables=tables,
        programmable_objects=objects,
        **kw,
    )


def _project(**kw) -> Project:
    report = kw.pop("report", None)
    project = Project(
        id=kw.pop("id", _UUID),
        name=kw.pop("name", "Sales migration"),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        assessment=(report.model_dump(mode="json") if report else None),
        **kw,
    )
    return project


def _plan_item(item_id: str, kind: ObjectKind, name: str, sql: str, **kw) -> dict:
    return {"id": item_id, "kind": kind.value, "name": name, "sql": sql, **kw}


# --- The delta filter -------------------------------------------------------------


def test_columns_carry_only_app_visible_changes():
    """A column that round-trips with nothing for an application to do is omitted —
    that filter is what keeps the section bounded on a wide database."""
    table = _table("Orders", [
        _col("Id", "int"),                                  # int -> integer: nothing to do
        _col("Name", "nvarchar", max_length=50),            # widening only
        _col("Qty", "tinyint"),                             # widens to smallint, no app change
        _col("Code", "nchar", max_length=6),                 # -> char(6): padding preserved
        _col("IsPaid", "bit"),                              # -> boolean: `= 1` breaks
        _col("Ref", "uniqueidentifier"),                    # -> uuid
        _col("Total", "money"),                             # -> numeric(19,4)
    ])
    bundle = build_bundle(_project(report=_report(tables=[table])))

    changed = {c.column for c in bundle.columns}
    assert changed == {"IsPaid", "Ref", "Total"}

    section = next(s for s in bundle.sections if s.name == "columns")
    assert section.count == 3
    assert section.omitted == 4       # the section is filtered, not empty


def test_type_changes_are_explained_once_in_the_glossary():
    """The per-column paragraph was the largest thing in the bundle, so a column
    points at a glossary key instead of carrying the prose."""
    tables = [
        _table(f"T{i}", [_col("Flag", "bit")]) for i in range(5)
    ]
    bundle = build_bundle(_project(report=_report(tables=tables)))

    assert [c.changes for c in bundle.columns] == [["bit"]] * 5
    assert list(bundle.change_glossary) == ["bit"]
    assert "boolean" in bundle.change_glossary["bit"]


def test_glossary_holds_exactly_the_keys_the_columns_use():
    table = _table("Customer", [
        _col("Id", "int"),
        _col("Flag", "bit"),
        _col("Name", "nvarchar", max_length=50, collation_name=CI_COLLATION),
    ])
    bundle = build_bundle(_project(report=_report(tables=[table])))

    used = {key for column in bundle.columns for key in column.changes}
    assert set(bundle.change_glossary) == used
    assert used == {"bit", rules.NONDETERMINISTIC_COLLATION}


# --- Collations: the trade, and what it costs application code ---------------------


def test_nondeterministic_collation_flags_pattern_matching():
    table = _table("Customer", [
        _col("Name", "nvarchar", max_length=50, collation_name=CI_COLLATION),
    ])
    bundle = build_bundle(_project(report=_report(tables=[table])))

    column = bundle.columns[0]
    assert column.rejects_pattern_match is True
    assert column.deterministic is False
    assert column.collation == "sql_latin1_general_cp1_ci_as"
    assert "LIKE" in bundle.change_glossary[rules.NONDETERMINISTIC_COLLATION]
    # The trade belongs in the bundle so a downstream agent does not "fix" it.
    assert any("Collations are mirrored" in t for t in bundle.operational.deliberate_trades)


def test_collation_trade_is_left_out_when_no_column_needs_it():
    """Static rationale ships only when its condition actually fired — otherwise it
    is advice about a decision this migration never made."""
    table = _table("Customer", [
        _col("Name", "nvarchar", max_length=50, collation_name=CS_COLLATION),
    ])
    bundle = build_bundle(_project(report=_report(tables=[table])))

    assert bundle.columns == []
    assert not any("Collations are mirrored" in t for t in bundle.operational.deliberate_trades)
    # Neither conditional trade applies here; the rest are unconditional.
    assert not any("Scratch collections" in t for t in bundle.operational.deliberate_trades)
    assert len(bundle.operational.deliberate_trades) == 2


# --- Scratch collections: the trade, and the rule that ships either way ------------


def test_scratch_collection_trade_ships_when_a_temp_table_was_rewritten():
    finding = Finding(
        rule_id="TEMP_TABLE", title="Temp table (#table) → fold into a CTE",
        severity=Severity.MEDIUM, object_name="dbo.usp_Report (PROCEDURE)",
        detail="#stage", recommendation="Fold it into the statement that reads it.",
    )
    bundle = build_bundle(_project(report=_report(findings=[finding])))

    trade = next(t for t in bundle.operational.deliberate_trades if "Scratch collections" in t)
    assert "must not reintroduce them" in trade


def test_the_temp_table_rewrite_rule_ships_even_when_no_object_used_one():
    """Application code can use a construct the database objects never did, so the
    rule is always listed — only `seen_in_source` says whether this database proved it."""
    bundle = build_bundle(_project(report=_report()))

    rule = next(r for r in bundle.rewrite_rules if r.tsql.startswith("#temp"))
    assert rule.seen_in_source is False
    assert "NOT a Postgres TEMP TABLE" in rule.postgres
    assert not any("Scratch collections" in t for t in bundle.operational.deliberate_trades)


# --- The expression join ----------------------------------------------------------
#
# Plan items carry `original` only for code objects, so DEFAULT/CHECK
# before-and-after has to come from the assessment, matched by plan item id.


def _expression_project(**kw) -> Project:
    table = _table(
        "Profile",
        [_col("PreferencesJson", "nvarchar", max_length=-1), _col("CreatedAt", "datetime2")],
        column_defaults=[ColumnDefaultInfo(column="CreatedAt", definition="(getdate())")],
        check_constraints=[CheckConstraintInfo(
            name="CK_Profile_Json",
            definition="([PreferencesJson] IS NULL OR isjson([PreferencesJson])=(1))",
        )],
    )
    plan = [
        _plan_item("default:dbo.Profile.CreatedAt", ObjectKind.CONSTRAINT,
                   "public.profile · DEFAULT CreatedAt",
                   'ALTER TABLE "public"."profile" ALTER COLUMN "CreatedAt" SET DEFAULT (now());'),
        _plan_item("check:dbo.Profile.CK_Profile_Json", ObjectKind.CONSTRAINT,
                   "public.profile · CHECK ck_profile_json",
                   'ALTER TABLE "public"."profile" ADD CONSTRAINT "ck_profile_json" CHECK '
                   '(("PreferencesJson" IS NULL OR isjson("PreferencesJson")=(1)));'),
    ]
    return _project(report=_report(tables=[table]), plan=plan, **kw)


def test_expression_join_reports_residual_tsql():
    bundle = build_bundle(_expression_project())

    assert len(bundle.expressions) == 1
    change = bundle.expressions[0]
    assert change.item_id == "check:dbo.Profile.CK_Profile_Json"
    assert change.kind == "check"
    assert change.source_expr.startswith("([PreferencesJson]")
    # Only identifier quoting changed, so the translator passed the shape through…
    assert change.passed_through is True
    # …and what it left behind is named, which is how the ISJSON class was caught.
    assert "isjson" in change.risk


def test_cleanly_translated_expressions_are_omitted_but_counted():
    """getdate() -> now() leaves an application nothing to do; the count keeps the
    filtering honest."""
    bundle = build_bundle(_expression_project())

    assert all(e.kind != "default" for e in bundle.expressions)
    section = next(s for s in bundle.sections if s.name == "expressions")
    assert (section.count, section.omitted) == (1, 1)


def test_filtered_index_predicates_join_too():
    table = _table(
        "Orders",
        [_col("Status", "nvarchar", max_length=20)],
        indexes=[IndexInfo(name="IX_Open", columns=[IndexColumnInfo(name="Status")],
                           filter_definition="([Status]='open')")],
    )
    plan = [_plan_item("index:dbo.Orders.IX_Open", ObjectKind.INDEX, "public.orders · ix_open",
                       'CREATE INDEX "orders_ix_open" ON "public"."orders" ("Status") '
                       "WHERE (\"Status\"='open');")]
    bundle = build_bundle(_project(report=_report(tables=[table]), plan=plan))

    change = next(e for e in bundle.expressions if e.kind == "index_filter")
    assert change.passed_through is True
    assert change.risk == ""


# --- Identifier casing ------------------------------------------------------------


def test_lowercase_policy_explains_that_names_changed():
    bundle = build_bundle(_project(
        report=_report(tables=[_table("Product", [_col("Id", "int")], schema="SalesLT")]),
        identifier_case=IdentifierCase.LOWERCASE,
    ))

    assert bundle.target.identifier_case == "lowercase"
    assert "lower-cased" in bundle.target.quoting_rule
    assert any(n.source == "SalesLT.Product" and n.target == "saleslt.product"
               for n in bundle.names)


def test_preserve_policy_demands_quoting_in_app_sql():
    """The opposite failure mode: unquoted app SQL folds to lower case and breaks."""
    bundle = build_bundle(_project(
        report=_report(tables=[_table("Product", [_col("Id", "int")], schema="SalesLT")]),
        identifier_case=IdentifierCase.PRESERVE,
    ))

    assert bundle.target.identifier_case == "preserve"
    assert "MUST quote" in bundle.target.quoting_rule
    assert not any(n.kind == "table" for n in bundle.names)   # the name did not change


def test_names_lists_derived_keys_and_the_trigger_companion():
    table = _table(
        "Orders", [_col("Id", "int")],
        primary_key=["Id"],
        indexes=[
            IndexInfo(name="IX_Ref", columns=[IndexColumnInfo(name="Id")], is_unique=True),
            IndexInfo(name="IX_Plain", columns=[IndexColumnInfo(name="Id")]),
        ],
    )
    trigger = ProgrammableObject(schema_name="dbo", object_name="trg_Orders",
                                 object_type="TRIGGER", line_count=5, definition="...")
    bundle = build_bundle(_project(report=_report(tables=[table], programmable_objects=[trigger])))

    by_kind = {}
    for change in bundle.names:
        by_kind.setdefault(change.kind, []).append(change)

    assert by_kind["constraint"][0].target == "pk_orders"
    # Unique indexes are named in ON CONFLICT and unique-violation messages; plain
    # ones follow the stated rule and are not listed one by one.
    assert [c.source for c in by_kind["index"]] == ["dbo.Orders.IX_Ref"]
    assert by_kind["trigger_function"][0].target == "public.trg_orders_fn"


# --- Callables --------------------------------------------------------------------


def test_callables_describe_the_call_site_change_and_provenance():
    plan = [
        _plan_item("procedure:dbo.GetOrders", ObjectKind.PROCEDURE, "public.get_orders",
                   "CREATE OR REPLACE PROCEDURE ...", reasoning="translated by the model"),
        _plan_item("view:dbo.OrderTotals", ObjectKind.VIEW, "public.order_totals",
                   "CREATE OR REPLACE VIEW ..."),
        _plan_item("function:dbo.Untranslated", ObjectKind.FUNCTION, "public.untranslated", ""),
    ]
    bundle = build_bundle(_project(report=_report(), plan=plan))

    by_type = {c.object_type: c for c in bundle.callables}
    assert "CALL public.get_orders" in by_type["PROCEDURE"].call_change
    assert by_type["PROCEDURE"].target_kind == "procedure"
    assert by_type["PROCEDURE"].provenance == "ai"
    assert by_type["VIEW"].provenance == "user-edited"      # sql present, no model reasoning
    assert by_type["FUNCTION"].translated is False
    assert by_type["FUNCTION"].provenance == "not-translated"

    section = next(s for s in bundle.sections if s.name == "callables")
    assert section.provenance == "mixed"


def test_a_procedure_translated_as_a_function_is_called_as_one():
    """The regression that broke a migrated app: the bundle described the *source*
    type, so a procedure reshaped into a function was still documented with `CALL`."""
    plan = [
        _plan_item(
            "procedure:dbo.usp_ItemReport", ObjectKind.PROCEDURE,
            "public.usp_itemreport",
            "CREATE OR REPLACE FUNCTION public.usp_itemreport(p_category text)\n"
            "RETURNS TABLE(item_id int) AS $$ BEGIN RETURN QUERY SELECT 1; END $$;",
            original="CREATE PROCEDURE dbo.usp_ItemReport AS BEGIN SELECT * FROM dbo.Items; END",
            reasoning="reshaped to a function because it returns rows",
        ),
    ]
    bundle = build_bundle(_project(report=_report(), plan=plan))

    call = bundle.callables[0]
    assert call.object_type == "PROCEDURE"          # what it was
    assert call.target_kind == "function"           # what it is
    assert call.returns_set is True
    assert "SELECT * FROM public.usp_itemreport(...)" in call.call_change
    assert "42809" in call.call_change
    assert "CALL public.usp_itemreport" not in call.call_change
    # Nothing is broken, so it is not a gap.
    assert not any(g.id.startswith("callable_shape:") for g in bundle.gaps)


def test_a_row_returning_procedure_left_as_a_procedure_is_a_high_gap():
    """A Postgres procedure cannot return a result set, so no application-side call
    form recovers the rows — it has to be reported as needing a database fix."""
    plan = [
        _plan_item(
            "procedure:dbo.usp_ItemReport", ObjectKind.PROCEDURE,
            "public.usp_itemreport",
            "CREATE OR REPLACE PROCEDURE public.usp_itemreport(p_category text)\n"
            "LANGUAGE plpgsql AS $$ BEGIN SELECT 1; END $$;",
            original="CREATE PROCEDURE dbo.usp_ItemReport AS BEGIN SELECT * FROM dbo.Items; END",
        ),
    ]
    bundle = build_bundle(_project(report=_report(), plan=plan))

    gap = next(g for g in bundle.gaps if g.id == "callable_shape:dbo.usp_ItemReport")
    assert gap.severity == "high"
    assert "42809" in gap.detail
    assert "RETURNS TABLE" in gap.recommendation
    # And the call site says so rather than implying CALL will do.
    assert "cannot serve its caller" in bundle.callables[0].call_change


def test_a_procedure_that_returns_nothing_is_unchanged():
    """The common case must not acquire a warning it does not need."""
    plan = [
        _plan_item(
            "procedure:dbo.usp_SetStatus", ObjectKind.PROCEDURE, "public.usp_setstatus",
            "CREATE OR REPLACE PROCEDURE public.usp_setstatus(p_id int) LANGUAGE plpgsql "
            "AS $$ BEGIN UPDATE orders SET status = 1 WHERE id = p_id; END $$;",
            original="CREATE PROCEDURE dbo.usp_SetStatus @Id int AS BEGIN UPDATE dbo.Orders "
                     "SET Status = 1 WHERE Id = @Id; END",
        ),
    ]
    bundle = build_bundle(_project(report=_report(), plan=plan))

    call = bundle.callables[0]
    assert call.target_kind == "procedure" and call.source_returns_result_set is False
    assert "CALL public.usp_setstatus" in call.call_change
    assert "cannot serve its caller" not in call.call_change
    assert bundle.gaps == []


# --- Rewrite rules ----------------------------------------------------------------


def test_rewrite_table_marks_the_rules_this_database_proved():
    findings = [
        Finding(rule_id="TOP", title="TOP clause", severity=Severity.LOW,
                object_name="dbo.GetTop (PROCEDURE)", detail="", recommendation=""),
    ]
    bundle = build_bundle(_project(report=_report(findings=findings)))

    by_tsql = {r.tsql: r for r in bundle.rewrite_rules}
    top = by_tsql["SELECT TOP n ..."]
    assert top.seen_in_source is True
    assert top.affected_objects == ["dbo.GetTop (PROCEDURE)"]
    # Rules that did not fire still ship: application code can use a construct the
    # database objects never did.
    assert any(r.seen_in_source is False for r in bundle.rewrite_rules)


def test_extra_rules_are_detected_in_scanned_expressions():
    """Constructs the rule engine never scans for, caught from expression text."""
    table = _table(
        "Audit", [_col("At", "datetime2")],
        column_defaults=[ColumnDefaultInfo(
            column="At",
            definition="(CONVERT([datetime2](3),(getdate() AT TIME ZONE 'E. South America Standard Time')))",
        )],
    )
    bundle = build_bundle(_project(report=_report(tables=[table])))

    seen = {r.tsql for r in bundle.rewrite_rules if r.seen_in_source}
    assert any("CONVERT" in t for t in seen)
    assert any("AT TIME ZONE" in t for t in seen)


# --- Gaps -------------------------------------------------------------------------


def test_high_findings_group_by_rule():
    """A wide database reports one rule against dozens of objects; a gap each would
    bury everything else."""
    findings = [
        Finding(rule_id="CURSOR", title="Cursor usage", severity=Severity.HIGH,
                object_name=f"dbo.P{i} (PROCEDURE)", detail="d", recommendation="r")
        for i in range(3)
    ] + [
        Finding(rule_id="TOP", title="TOP clause", severity=Severity.LOW,
                object_name="dbo.P9 (PROCEDURE)", detail="", recommendation=""),
    ]
    bundle = build_bundle(_project(report=_report(findings=findings)))

    assessment_gaps = [g for g in bundle.gaps if g.origin == "assessment"]
    assert len(assessment_gaps) == 1
    assert assessment_gaps[0].id == "assessment:CURSOR"
    assert len(assessment_gaps[0].affected) == 3


def test_validation_misses_become_gaps_and_matches_do_not():
    validation = ValidationReport(
        source_database="SalesDB", target_database="databricks_postgres",
        target_schema="public", match_score=80,
        items=[
            ValidationItem(id="table:dbo.Gone", kind=ObjectKind.TABLE, target_name="public.gone",
                           status=MatchStatus.MISSING, severity=Severity.HIGH,
                           detail="not in target", recommendation="re-apply"),
            ValidationItem(id="table:dbo.Fine", kind=ObjectKind.TABLE, target_name="public.fine",
                           status=MatchStatus.MATCHED),
        ],
    )
    bundle = build_bundle(_project(report=_report(), validation=validation.model_dump(mode="json")))

    gaps = [g for g in bundle.gaps if g.origin == "validation"]
    assert [g.id for g in gaps] == ["validation:table:dbo.Gone"]
    assert any("80%" in c for c in bundle.provenance.completeness)


def test_ai_assessment_is_excluded_and_says_so():
    """It was produced before the migration ran, so it warns about risks that were
    then handled and recommends approaches the migration rejected (citext,
    deterministic collations). Carrying it here would contradict the trades this
    very artifact exists to protect."""
    ai = AIAssessment(
        success=True, summary="prose",
        risks=[AIRisk(title="Use citext for CI columns", severity="high",
                      rationale="why", recommendation="switch to citext")],
    )
    bundle = build_bundle(_project(report=_report(ai_assessment=ai)))

    assert [g for g in bundle.gaps if g.origin == "ai"] == []
    serialized = bundle.model_dump_json()
    assert "citext" not in serialized.replace(
        # The trade names citext only to forbid it.
        "or substitute citext", "",
    )


# --- Redaction --------------------------------------------------------------------


def test_the_bundle_carries_no_coordinates_or_secrets():
    """It travels to another repo or agent. How the *migration tool* connected says
    nothing about how the application should, and a hostname in the export is
    disclosure with nothing gained."""
    project = _project(name="Prod DB on sqlserver-prod-01", report=_report(database="SalesDB"))
    project.target.host = "instance.database.cloud.databricks.com"
    project.target.user = "svc@example.com"
    project.target.secret_ref = SecretRef(scope="customer-vault", key="pg-password")

    serialized = build_bundle(project).model_dump_json()

    for detail in ("instance.database.cloud.databricks.com", "svc@example.com",
                   "customer-vault", "sqlserver-prod-01", "SalesDB"):
        assert detail not in serialized


# --- Honesty about what the bundle knows ------------------------------------------


def test_readiness_score_ships_with_its_formula():
    """The score is a clamped penalty sum: 0 with no HIGH findings is possible, and
    a consumer given the bare number concludes the migration failed."""
    report = _report(readiness_score=0,
                     severity_counts={"info": 29, "low": 33, "medium": 21, "high": 0})
    bundle = build_bundle(_project(report=report))

    assert bundle.source_summary.readiness_score == 0
    assert bundle.source_summary.severity_counts["high"] == 0
    assert "clamped" in bundle.source_summary.score_formula


def test_completeness_names_the_phases_that_have_not_run():
    bundle = build_bundle(_project(report=_report()))
    completeness = " ".join(bundle.provenance.completeness)

    assert "Validation has not been run" in completeness
    assert "Query parity has not been run" in completeness
    assert "No migration plan" in completeness


def test_a_project_without_an_assessment_still_exports_guidance():
    bundle = build_bundle(_project())

    assert bundle.columns == []
    assert bundle.target.quoting_rule               # static guidance survives
    assert "No assessment is stored" in bundle.provenance.completeness[0]


def test_sections_agree_with_the_body():
    table = _table("Orders", [_col("IsPaid", "bit")], primary_key=["IsPaid"])
    bundle = build_bundle(_project(report=_report(tables=[table])))
    counts = {s.name: s.count for s in bundle.sections}

    assert counts["columns"] == len(bundle.columns)
    assert counts["names"] == len(bundle.names)
    assert counts["gaps"] == len(bundle.gaps)
    assert counts["rewrite_rules"] == len(bundle.rewrite_rules)
    assert counts["expressions"] == len(bundle.expressions)
    assert bundle.start_here.startswith("This is a migration context bundle")


def test_transient_sqlstates_come_from_the_connector():
    """They replace the app's SQL Server error-number logic, so they must be the
    codes the migration itself retries on."""
    from backend.connectors.lakebase import TRANSIENT_SQLSTATES

    bundle = build_bundle(_project(report=_report()))

    assert set(bundle.operational.transient_sqlstates) == set(TRANSIENT_SQLSTATES)
    assert "40P01" in bundle.operational.transient_sqlstates


# --- Scale ------------------------------------------------------------------------


def test_bundle_stays_bounded_on_a_wide_database():
    """The delta filter is the only thing keeping this in budget: the same project
    dumped whole is megabytes."""
    tables = []
    for t in range(200):
        columns = [_col(f"c{i}", "int") for i in range(18)]
        columns.append(_col("IsPaid", "bit"))
        columns.append(_col("Name", "nvarchar", max_length=50, collation_name=CI_COLLATION))
        tables.append(_table(f"Table{t}", columns, primary_key=["c0"]))

    bundle = build_bundle(_project(report=_report(tables=tables)))
    size = len(bundle.model_dump_json())

    assert len(bundle.columns) == 400                      # 2 changed per table
    assert next(s for s in bundle.sections if s.name == "columns").omitted == 3600
    assert size < 500_000, f"bundle grew to {size:,} bytes"


# --- Route ------------------------------------------------------------------------


def _client(monkeypatch, store) -> TestClient:
    monkeypatch.setattr(context_routes, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(context_routes.router)
    return TestClient(app)


def test_route_returns_the_bundle(tmp_path, monkeypatch):
    project = _project(report=_report(tables=[_table("Orders", [_col("IsPaid", "bit")])]))
    project.target.host = "instance.database.cloud.databricks.com"
    store = LocalFileStore(str(tmp_path))
    store.save(project)
    client = _client(monkeypatch, store)

    body = client.get(f"/api/projects/{_UUID}/context-bundle").json()
    assert body["provenance"]["project_id"] == _UUID
    assert body["provenance"]["tool_version"]
    assert [c["column"] for c in body["columns"]] == ["IsPaid"]
    assert "instance.database.cloud.databricks.com" not in client.get(
        f"/api/projects/{_UUID}/context-bundle").text


def test_route_404s_for_an_unknown_project(tmp_path, monkeypatch):
    client = _client(monkeypatch, LocalFileStore(str(tmp_path)))

    response = client.get("/api/projects/does-not-exist/context-bundle")
    assert response.status_code == 404
