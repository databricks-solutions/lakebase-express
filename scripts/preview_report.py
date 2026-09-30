"""A fully-populated migration report, for eyeballing the print layout without a
real migration.

    PYTHONPATH=. python3 scripts/preview_report.py [out.html]   # write the HTML
    PYTHONPATH=. python3 scripts/preview_report.py --serve       # seed + serve the app

``--serve`` seeds the fixture into a throwaway project store and runs the app on
:8000, so the Migration Report module can be driven in the browser. Both modes need
no Databricks workspace and no database.
"""
from __future__ import annotations

import os
import pathlib
import sys

from backend.assessment.models import (
    AIAssessment,
    AIRisk,
    ColumnInfo,
    Finding,
    ProgrammableObject,
    Severity,
    TableInfo,
)
from backend.migration.models import ObjectKind
from backend.projects.models import PhaseStatus, Project, SourceConfig, TargetConfig
from backend.query_parity.models import (
    ParityStatus,
    QueryComparison,
    QueryParityReport,
    SideResult,
    SyntheticQuery,
)
from backend.report.builder import build_report
from backend.report.html import render_report
from backend.run_store import get_run_store
from backend.validation.models import (
    MatchStatus,
    ObjectDiff,
    ValidationItem,
    ValidationReport,
)

_PROJECT_ID = "3f2b9c10-5a44-4f0e-9b31-6d8a0c7e1234"


def _table(name: str, rows: int, columns: list[str]) -> TableInfo:
    return TableInfo(
        schema_name="SalesLT",
        table_name=name,
        row_count=rows,
        column_count=len(columns),
        columns=[ColumnInfo(name=c, data_type="nvarchar", max_length=60) for c in columns],
        primary_key=[columns[0]],
    )


def _assessment() -> dict:
    tables = [
        _table("Customer", 19_820, ["CustomerID", "FirstName", "EmailAddress"]),
        _table("SalesOrderDetail", 542_310, ["SalesOrderDetailID", "OrderQty", "LineTotal"]),
        _table("Product", 2_950, ["ProductID", "Name", "ListPrice"]),
        _table("Address", 11_004, ["AddressID", "City", "PostalCode"]),
    ]
    objects = [
        ProgrammableObject(schema_name="SalesLT", object_name=name, object_type=kind,
                           line_count=40, definition="SELECT 1")
        for name, kind in (
            ("uspGetCustomerOrders", "PROCEDURE"),
            ("uspRebuildTotals", "PROCEDURE"),
            ("vProductAndDescription", "VIEW"),
            ("ufnGetSalesOrderStatusText", "FUNCTION"),
            ("trgCustomerAudit", "TRIGGER"),
        )
    ]
    findings = [
        Finding(rule_id="CURSOR", title="Cursor requires a manual rewrite",
                severity=Severity.HIGH, object_name=f"SalesLT.{name}",
                detail="A T-SQL cursor has no mechanical PL/pgSQL equivalent.",
                recommendation="Rewrite as a set-based statement, or a FOR loop over a query.")
        for name in ("uspRebuildTotals", "uspGetCustomerOrders")
    ] + [
        Finding(rule_id="COLLATION_INSENSITIVE",
                title="Case-insensitive collation becomes nondeterministic",
                severity=Severity.MEDIUM, object_name="SalesLT.Customer.EmailAddress",
                detail="PostgreSQL rejects LIKE and regex on a nondeterministic collation.",
                recommendation="Use COLLATE \"C\" with ILIKE at the call site."),
        Finding(rule_id="TYPE_DATETIME", title="datetime loses no precision but changes type",
                severity=Severity.LOW, object_name="SalesLT.Address.ModifiedDate",
                detail="datetime maps to timestamp(3).", recommendation="No action needed."),
    ]
    return {
        "database": "AdventureWorksLT",
        "table_count": len(tables),
        "total_rows": sum(t.row_count for t in tables),
        "programmable_object_count": len(objects),
        "findings": [f.model_dump(mode="json") for f in findings],
        "readiness_score": 75,
        "severity_counts": {"info": 3, "low": 1, "medium": 1, "high": 2},
        "tables": [t.model_dump(mode="json") for t in tables],
        "programmable_objects": [o.model_dump(mode="json") for o in objects],
        "ai_assessment": AIAssessment(
            summary="A small schema whose risk is concentrated in two cursor-based "
                    "procedures and one case-insensitive email column.",
            complexity="Medium",
            complexity_rationale="Most tables map mechanically; the stored logic does not.",
            estimated_effort="3-5 engineer-days including application changes.",
            risks=[
                AIRisk(title="Row-by-row totals rebuild will not scale",
                       category="performance", severity="high",
                       affected_objects="SalesLT.uspRebuildTotals",
                       rationale="The cursor commits per row, which on Lakebase costs a "
                                 "round trip each time.",
                       recommendation="Rewrite as a single UPDATE ... FROM."),
                AIRisk(title="Email uniqueness depends on case-insensitive comparison",
                       category="data-integrity", severity="medium",
                       affected_objects="SalesLT.Customer.EmailAddress",
                       rationale="The unique index collides differently under a "
                                 "deterministic collation.",
                       recommendation="Keep the mirrored collation; do not switch to citext."),
            ],
            recommendations=[
                "Rewrite both cursor procedures before the cutover rehearsal.",
                "Grep the application for LIKE against EmailAddress.",
            ],
            endpoint="databricks-claude-sonnet-4-5",
            success=True,
        ).model_dump(mode="json"),
    }


def _plan() -> list[dict]:
    items = [
        {"id": "schema:SalesLT", "kind": ObjectKind.SCHEMA.value, "name": "saleslt",
         "sql": "CREATE SCHEMA saleslt", "notes": ""},
        {"id": "collation:SQL_Latin1_General_CP1_CI_AS", "kind": ObjectKind.COLLATION.value,
         "name": "saleslt.sql_latin1_general_cp1_ci_as", "sql": "CREATE COLLATION ...",
         "notes": ""},
    ]
    items += [
        {"id": f"table:SalesLT.{name}", "kind": ObjectKind.TABLE.value,
         "name": f"saleslt.{name.lower()}", "sql": f"CREATE TABLE saleslt.{name.lower()} ()",
         "notes": ""}
        for name in ("Customer", "SalesOrderDetail", "Product", "Address")
    ]
    items += [
        {"id": "procedure:SalesLT.uspGetCustomerOrders", "kind": ObjectKind.PROCEDURE.value,
         "name": "saleslt.uspgetcustomerorders",
         "sql": "CREATE OR REPLACE FUNCTION saleslt.uspgetcustomerorders(p_id int) "
                "RETURNS TABLE(order_id int) AS $$ ... $$ LANGUAGE plpgsql",
         "original": "CREATE PROCEDURE ... SELECT * FROM ...",
         "reasoning": "The source returns a result set, so it has to be a function.",
         "notes": "Callers move from EXEC to SELECT * FROM."},
        {"id": "procedure:SalesLT.uspRebuildTotals", "kind": ObjectKind.PROCEDURE.value,
         "name": "saleslt.usprebuildtotals", "sql": "", "original": "CREATE PROCEDURE ...",
         "notes": "Cursor could not be translated."},
        {"id": "view:SalesLT.vProductAndDescription", "kind": ObjectKind.VIEW.value,
         "name": "saleslt.vproductanddescription",
         "sql": "CREATE OR REPLACE VIEW saleslt.vproductanddescription AS SELECT 1",
         "reasoning": "Mechanical.", "notes": ""},
        {"id": "function:SalesLT.ufnGetSalesOrderStatusText",
         "kind": ObjectKind.FUNCTION.value, "name": "saleslt.ufngetsalesorderstatustext",
         "sql": "CREATE OR REPLACE FUNCTION saleslt.ufngetsalesorderstatustext(s smallint) "
                "RETURNS text AS $$ ... $$ LANGUAGE plpgsql",
         "notes": "Edited by hand to widen the input type."},
        {"id": "trigger:SalesLT.trgCustomerAudit", "kind": ObjectKind.TRIGGER.value,
         "name": "saleslt.trgcustomeraudit",
         "sql": "CREATE TRIGGER trgcustomeraudit AFTER UPDATE ON saleslt.customer ...",
         "reasoning": "Companion function added.", "notes": ""},
    ]
    items += [
        {"id": f"constraint:SalesLT.{name}", "kind": ObjectKind.CONSTRAINT.value,
         "name": f"pk_{name.lower()}", "sql": "ALTER TABLE ...", "notes": ""}
        for name in ("Customer", "SalesOrderDetail", "Product", "Address")
    ]
    items += [
        {"id": "index:SalesLT.Customer.IX_Email", "kind": ObjectKind.INDEX.value,
         "name": "customer_ix_email", "sql": "CREATE UNIQUE INDEX ...", "notes": ""},
        {"id": "foreign_key:SalesLT.SalesOrderDetail.FK_Product",
         "kind": ObjectKind.FOREIGN_KEY.value, "name": "salesorderdetail_fk_product",
         "sql": "ALTER TABLE ...", "notes": ""},
    ]
    return items


def _validation() -> dict:
    return ValidationReport(
        source_database="AdventureWorksLT",
        target_database="databricks_postgres",
        target_schema="public",
        generated_at="2026-09-29T09:14:02+00:00",
        match_score=88,
        total_source=17,
        matched=15, missing=1, mismatched=1, extra=0,
        source_rows=576_084, target_rows=576_084,
        tables_compared=4, tables_estimated=1,
        items=[
            ValidationItem(
                id="procedure:SalesLT.uspRebuildTotals", kind=ObjectKind.PROCEDURE,
                source_name="SalesLT.uspRebuildTotals", target_name="saleslt.usprebuildtotals",
                status=MatchStatus.MISSING, severity=Severity.HIGH,
                detail="Not found in the target — its cursor was never translated.",
                recommendation="Rewrite it by hand, or keep the logic in the application.",
            ),
            ValidationItem(
                id="table:SalesLT.Customer", kind=ObjectKind.TABLE,
                source_name="SalesLT.Customer", target_name="saleslt.customer",
                status=MatchStatus.MISMATCH, severity=Severity.MEDIUM,
                detail="One column's collation does not match the source's.",
                recommendation="Re-apply the column's COLLATE clause, then re-run validation.",
                source_rows=19_820, target_rows=19_820,
                collation_drift=["emailaddress: expected sql_latin1_general_cp1_ci_as, found default"],
            ),
            ValidationItem(
                id="index:SalesLT.Product", kind=ObjectKind.INDEX,
                source_name="SalesLT.Product", target_name="saleslt.product",
                status=MatchStatus.MATCHED, severity=Severity.INFO,
                objects_expected=3, objects_present=3,
                objects=[ObjectDiff(name="product_ix_name", status=MatchStatus.MATCHED)],
            ),
        ],
    ).model_dump(mode="json")


def _parity() -> dict:
    def pair(qid, title, category, status, src_rows, tgt_rows, src_ms, tgt_ms, **kw):
        return QueryComparison(
            query=SyntheticQuery(id=qid, title=title, category=category,
                                 intent=kw.pop("intent", ""),
                                 source_sql="SELECT 1", target_sql="SELECT 1"),
            source=SideResult(ok=True, row_count=src_rows, duration_ms=src_ms,
                              error=kw.pop("source_error", None)),
            target=SideResult(ok=True, row_count=tgt_rows, duration_ms=tgt_ms,
                              error=kw.pop("target_error", None)),
            status=status,
            count_match=src_rows == tgt_rows,
            format_match=kw.pop("format_match", src_rows == tgt_rows),
            speedup_ratio=round(tgt_ms / src_ms, 2) if src_ms else None,
            **kw,
        )

    return QueryParityReport(
        source_database="AdventureWorksLT", target_database="databricks_postgres",
        target_schema="public", generated_at="2026-09-29T09:31:44+00:00",
        endpoint="databricks-claude-sonnet-4-5",
        requested=5, total=5, matched=3, mismatched=1, errored=1, parity_score=60,
        source_total_ms=4_820, target_total_ms=1_940,
        comparisons=[
            pair("q1", "Top customers by order value", "aggregation",
                 ParityStatus.MATCH, 10, 10, 1_420, 480,
                 detail="Same 10 rows in the same order."),
            pair("q2", "Orders per month", "aggregation", ParityStatus.MATCH, 24, 24, 910, 300,
                 detail="Identical."),
            pair("q3", "Products never ordered", "join", ParityStatus.MATCH, 42, 42, 1_180, 610,
                 detail="Identical."),
            pair("q4", "Customers matched by email pattern", "filter",
                 ParityStatus.MISMATCH, 118, 96, 1_310, 550,
                 detail="The target returned 22 fewer rows: LIKE on a nondeterministic "
                        "collation matched differently.",
                 mismatch_columns=["emailaddress"], format_match=False),
            pair("q5", "Running total by territory", "window", ParityStatus.ERROR,
                 0, 0, 0, 0,
                 target_error='function saleslt.usprebuildtotals(integer) does not exist',
                 detail="Failed on the target."),
        ],
    ).model_dump(mode="json")


def _seed_runs() -> None:
    store = get_run_store()
    store.save("sync_run", "8c1f2d30-7b21-4a55-8d90-11aa22bb33cc", "partial", {
        "run_id": "8c1f2d30-7b21-4a55-8d90-11aa22bb33cc",
        "project_id": _PROJECT_ID,
        "status": "partial",
        "started_at": "2026-09-29T08:40:11+00:00",
        "finished_at": "2026-09-29T08:52:47+00:00",
        "tables": [
            {"name": "SalesLT.Customer", "target": "saleslt.customer", "status": "success",
             "rows_copied": 19_820, "total_rows": 19_820},
            {"name": "SalesLT.SalesOrderDetail", "target": "saleslt.salesorderdetail",
             "status": "success", "rows_copied": 542_310, "total_rows": 542_310},
            {"name": "SalesLT.Product", "target": "saleslt.product", "status": "success",
             "rows_copied": 2_950, "total_rows": 2_950},
            {"name": "SalesLT.Address", "target": "saleslt.address", "status": "failed",
             "rows_copied": 11_004, "total_rows": 11_004,
             "error": 'null value in column "city" violates not-null constraint'},
        ],
    }, _PROJECT_ID)
    store.save("async_job", "5d6e7f80-1122-4333-8444-555566667777", "scheduled", {
        "run_id": "5d6e7f80-1122-4333-8444-555566667777",
        "status": "scheduled", "tables_total": 4, "scheduled": True,
        "quartz_cron": "0 0 3 * * ?",
        "job_url": "https://example-workspace.cloud.databricks.com/jobs/123456789",
    }, _PROJECT_ID)


def build_project() -> Project:
    return Project(
        id=_PROJECT_ID,
        name="AdventureWorks to Lakebase",
        source_connector_id="azure-sql",
        created_at="2026-09-20T10:00:00+00:00",
        updated_at="2026-09-29T09:32:00+00:00",
        source=SourceConfig(source_type="azure-sql", host="example-sql.database.windows.net",
                            database="AdventureWorksLT", username="migrator"),
        target=TargetConfig(host="example-instance.database.cloud.databricks.com",
                            database="databricks_postgres", user="migrator@example.com"),
        target_schema="saleslt",
        assessment=_assessment(),
        plan=_plan(),
        selection=["SalesLT.Customer", "SalesLT.SalesOrderDetail", "SalesLT.Product",
                   "SalesLT.Address"],
        validation=_validation(),
        query_parity=_parity(),
        statuses={
            "assessment": PhaseStatus.DONE, "sizing": PhaseStatus.DONE,
            "schema": PhaseStatus.DONE, "data": PhaseStatus.DONE,
            "validation": PhaseStatus.IN_PROGRESS,
        },
    )


def serve() -> None:
    """Seed the fixture into a throwaway store and run the app, so the module can be
    driven in a browser. Runs must be seeded in *this* process: the default run store
    is process memory."""
    import uvicorn

    os.environ.setdefault("LBX_PROJECTS_DIR", "/tmp/lbx-preview-projects")
    from backend.main import app
    from backend.projects.store import get_store

    _seed_runs()
    get_store().save(build_project())
    print(f"Seeded {_PROJECT_ID} into {os.environ['LBX_PROJECTS_DIR']}")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


def main() -> None:
    if "--serve" in sys.argv:
        serve()
        return
    _seed_runs()
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/lbx-report.html")
    report = build_report(build_project(), tool_version="0.1.0")
    out.write_text(render_report(report), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
