"""Post-migration validation: comparison logic, AI fixer parsing, run registry."""
import json
from types import SimpleNamespace

from backend.assessment.models import (
    CheckConstraintInfo,
    ColumnDefaultInfo,
    ColumnInfo,
    ConnectionRequest,
    ForeignKeyInfo,
    IndexColumnInfo,
    IndexInfo,
    ProgrammableObject,
    Severity,
    TableInfo,
)
from backend.migration.models import LakebaseConnRequest, ObjectKind
from backend.validation import fixer, runs
from backend.validation.comparator import (
    TargetInventory,
    TargetObject,
    compare,
    expected_schemas,
    fetch_target_inventory,
    merge_object_rescan,
    run_validation,
)
from backend.validation.models import (
    MatchStatus,
    ValidationItem,
    ValidationReport,
    ValidationRunRequest,
    ValidationRunState,
)


def _table(schema="dbo", name="Orders", rows=100, cols=None):
    cols = cols or [
        ColumnInfo(name="Id", data_type="int", is_nullable=False),
        ColumnInfo(name="Total", data_type="money"),
    ]
    return TableInfo(schema_name=schema, table_name=name, row_count=rows,
                     column_count=len(cols), columns=cols, primary_key=["Id"])


def _proc(schema="dbo", name="usp_Report", definition="CREATE PROCEDURE dbo.usp_Report AS SELECT 1"):
    return ProgrammableObject(schema_name=schema, object_name=name, object_type="PROCEDURE",
                              line_count=1, definition=definition)


def _by_id(report):
    return {i.id: i for i in report.items}


# --- compare(): existence -----------------------------------------------------------


def test_fully_matched_table_scores_100():
    inv = TargetInventory(
        schemas={"public"},
        tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer", "Total": "numeric"}},
    )
    rep = compare([_table()], [], inv,
                  source_counts={("dbo", "Orders"): 100}, target_counts={("dbo", "Orders"): 100})
    items = _by_id(rep)
    assert items["schema:dbo"].status is MatchStatus.MATCHED
    t = items["table:dbo.Orders"]
    assert t.status is MatchStatus.MATCHED and t.severity is Severity.INFO
    assert t.source_rows == 100 and t.target_rows == 100
    assert rep.match_score == 100 and rep.missing == 0 and rep.mismatched == 0
    assert rep.source_rows == rep.target_rows == 100 and rep.tables_compared == 1


def test_missing_table_gets_create_fix():
    rep = compare([_table()], [], TargetInventory(schemas={"public"}))
    t = _by_id(rep)["table:dbo.Orders"]
    assert t.status is MatchStatus.MISSING and t.severity is Severity.HIGH
    assert t.target_name == "public.orders"
    assert 'CREATE TABLE IF NOT EXISTS "public"."orders"' in t.fix_sql
    # Score is the plain matched percentage: schema matched, table missing → 1/2.
    assert rep.missing == 1 and rep.match_score == 50


def test_missing_schema_gets_create_fix():
    rep = compare([_table(schema="SalesLT", name="Product")], [], TargetInventory(schemas={"public"}))
    s = _by_id(rep)["schema:SalesLT"]
    assert s.status is MatchStatus.MISSING
    assert s.fix_sql == 'CREATE SCHEMA IF NOT EXISTS "saleslt";'


def test_missing_procedure_carries_source_definition():
    definition = "CREATE PROCEDURE dbo.usp_Report AS SELECT 1"
    rep = compare([], [_proc(definition=definition)], TargetInventory(schemas={"public"}))
    p = _by_id(rep)["procedure:dbo.usp_Report"]
    assert p.status is MatchStatus.MISSING and p.kind is ObjectKind.PROCEDURE
    assert p.target_name == "public.usp_report"
    assert p.source_definition == definition
    assert "AI fix" in p.recommendation


def test_validation_matches_case_preserved_targets_exactly():
    inv = TargetInventory(
        schemas={"SalesLT"},
        tables={("SalesLT", "Product")},
        columns={("SalesLT", "Product"): {"Id": "integer", "Total": "numeric"}},
        procedures={("SalesLT", "GetProducts")},
    )
    report = compare(
        [_table(schema="SalesLT", name="Product")],
        [_proc(schema="SalesLT", name="GetProducts")],
        inv,
        identifier_case="preserve",
    )
    assert _by_id(report)["table:SalesLT.Product"].status is MatchStatus.MATCHED
    proc = _by_id(report)["procedure:SalesLT.GetProducts"]
    assert proc.status is MatchStatus.MATCHED
    assert proc.target_name == "SalesLT.GetProducts"


def test_matched_procedure_has_no_definition_payload():
    inv = TargetInventory(schemas={"public"}, procedures={("public", "usp_report")})
    p = _by_id(compare([], [_proc()], inv))["procedure:dbo.usp_Report"]
    assert p.status is MatchStatus.MATCHED and p.source_definition == ""


# --- compare(): structure + rows ------------------------------------------------------


def test_row_count_mismatch_is_high_severity():
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer", "Total": "numeric"}},
    )
    rep = compare([_table()], [], inv,
                  source_counts={("dbo", "Orders"): 100}, target_counts={("dbo", "Orders"): 97})
    t = _by_id(rep)["table:dbo.Orders"]
    assert t.status is MatchStatus.MISMATCH and t.severity is Severity.HIGH
    assert "row counts differ" in t.detail.lower()
    assert t.fix_sql == ""  # data problems aren't fixed by DDL


def test_missing_column_gets_alter_fix():
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer"}},  # Total missing
    )
    t = _by_id(compare([_table()], [], inv))["table:dbo.Orders"]
    assert t.status is MatchStatus.MISMATCH
    assert t.columns_missing == ["Total"]
    assert 'ADD COLUMN "Total" numeric(19,4)' in t.fix_sql
    assert "Total" in t.detail  # column names keep their casing in the detail


def test_column_type_drift_detected_with_normalization():
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer", "Total": "text"}},
    )
    t = _by_id(compare([_table()], [], inv))["table:dbo.Orders"]
    assert t.status is MatchStatus.MISMATCH
    assert t.type_drift == ["Total: expected numeric, found text"]


def test_row_totals_only_cover_tables_counted_on_both_sides():
    # A table missing in the target used to inflate the source total with the
    # scanner's approximate estimate while adding nothing to the target total —
    # the hero line then compared different table sets.
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer", "Total": "numeric"}},
    )
    tables = [_table(), _table(name="Huge", rows=500_000_000)]  # Huge missing in target
    rep = compare(tables, [], inv,
                  source_counts={("dbo", "Orders"): 100}, target_counts={("dbo", "Orders"): 100})
    assert _by_id(rep)["table:dbo.Huge"].status is MatchStatus.MISSING
    assert rep.tables_compared == 1
    assert rep.source_rows == rep.target_rows == 100  # Huge's estimate excluded


def test_approximate_counts_within_tolerance_match_and_are_flagged():
    # A huge table counted by estimate on both sides: reltuples drifts slightly
    # from the true count, so a sub-tolerance gap must still read as matched —
    # and the item/report must disclose that the numbers are approximate.
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "position")},
        columns={("public", "position"): {"Id": "integer", "Total": "numeric"}},
    )
    rep = compare([_table(name="Position", rows=493_249_680)], [], inv,
                  source_counts={("dbo", "Position"): 493_249_680},
                  target_counts={("dbo", "Position"): 493_100_000},  # ~0.03% off
                  approximate_counts={("dbo", "Position")})
    t = _by_id(rep)["table:dbo.Position"]
    assert t.status is MatchStatus.MATCHED and t.rows_approximate is True
    assert t.source_rows == 493_249_680 and t.target_rows == 493_100_000
    assert "estimated" in t.detail.lower()
    # The big table is now included in the totals and counted as estimated.
    assert rep.tables_compared == 1 and rep.tables_estimated == 1
    assert rep.source_rows == 493_249_680 and rep.target_rows == 493_100_000


def test_approximate_counts_beyond_tolerance_still_mismatch():
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "position")},
        columns={("public", "position"): {"Id": "integer", "Total": "numeric"}},
    )
    rep = compare([_table(name="Position", rows=493_249_680)], [], inv,
                  source_counts={("dbo", "Position"): 493_249_680},
                  target_counts={("dbo", "Position"): 400_000_000},  # ~19% short
                  approximate_counts={("dbo", "Position")})
    t = _by_id(rep)["table:dbo.Position"]
    assert t.status is MatchStatus.MISMATCH and t.severity is Severity.HIGH
    assert "approx." in t.detail.lower()


def test_failed_target_count_is_unverified_not_matched():
    # The reported bug: the target COUNT(*) failed, leaving the source counted
    # but the target unset. That must NOT read as "matched" — the source key is
    # present, so we know a count pass ran and simply couldn't verify the target.
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "position")},
        columns={("public", "position"): {"Id": "integer", "Total": "numeric"}},
    )
    rep = compare([_table(name="Position", rows=493_249_680)], [], inv,
                  source_counts={("dbo", "Position"): 493_249_680},
                  target_counts={})  # target count neither counted nor estimated
    t = _by_id(rep)["table:dbo.Position"]
    assert t.status is MatchStatus.MISMATCH
    assert "could not verify" in t.detail.lower()
    assert "re-run" in t.recommendation.lower()
    # An unverified table has no target number, so it stays out of the totals
    # rather than inflating the source side alone.
    assert rep.tables_compared == 0


def test_varchar_normalizes_to_character_varying():
    cols = [ColumnInfo(name="Name", data_type="nvarchar", max_length=50)]
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "orders")},
        columns={("public", "orders"): {"Name": "character varying"}},
    )
    t = _by_id(compare([_table(cols=cols)], [], inv))["table:dbo.Orders"]
    assert t.status is MatchStatus.MATCHED and t.type_drift == []


# --- compare(): extras ----------------------------------------------------------------


def test_extra_table_gets_guarded_drop_fix():
    inv = TargetInventory(schemas={"public"}, tables={("public", "orders"), ("public", "zombie")},
                          columns={("public", "orders"): {"Id": "integer", "Total": "numeric"}})
    rep = compare([_table()], [], inv)
    extra = _by_id(rep)["extra-table:public.zombie"]
    assert extra.status is MatchStatus.EXTRA and extra.severity is Severity.LOW
    assert extra.source_name == ""
    assert 'DROP TABLE IF EXISTS "public"."zombie";' in extra.fix_sql
    assert "Destructive" in extra.fix_sql
    assert rep.extra == 1


def test_extra_function_hints_at_trigger_helpers():
    inv = TargetInventory(schemas={"public"}, functions={("public", "trg_orders_fn")})
    extra = _by_id(compare([], [], inv))["extra-function:public.trg_orders_fn"]
    assert "trigger function" in extra.recommendation


def test_migration_created_trigger_function_matches_and_explains_the_split():
    # A source trigger becomes a Postgres trigger + a <trigger>_fn function the
    # migration creates. The companion function must not surface as "extra"; it
    # is a matched function whose detail explains the two-object Postgres model.
    trg = ProgrammableObject(schema_name="dbo", object_name="trg_Order_Audit",
                             object_type="TRIGGER", line_count=1,
                             definition="CREATE TRIGGER trg_Order_Audit ...")
    inv = TargetInventory(
        schemas={"public"},
        triggers={("public", "trg_order_audit")},
        functions={("public", "trg_order_audit_fn")},
    )
    rep = compare([], [trg], inv)
    ids = _by_id(rep)
    assert "extra-function:public.trg_order_audit_fn" not in ids
    assert ids["trigger:dbo.trg_Order_Audit"].status is MatchStatus.MATCHED
    assert rep.extra == 0

    fn = ids["trigger-fn:dbo.trg_Order_Audit"]
    assert fn.kind is ObjectKind.FUNCTION and fn.status is MatchStatus.MATCHED
    assert fn.source_name == "" and fn.target_name == "public.trg_order_audit_fn"
    # The detail teaches the SQL Server user why the target has a function the
    # source never had.
    assert "two objects" in fn.detail and "trg_Order_Audit" in fn.detail
    assert rep.matched == 3  # schema (dbo->public) + trigger + its companion function


def test_trigger_function_matched_with_preserved_case():
    trg = ProgrammableObject(schema_name="custody", object_name="trg_CashBalance_Guard",
                             object_type="TRIGGER", line_count=1, definition="...")
    inv = TargetInventory(
        schemas={"custody"},
        triggers={("custody", "trg_CashBalance_Guard")},
        functions={("custody", "trg_CashBalance_Guard_fn")},
    )
    rep = compare([], [trg], inv, identifier_case="preserve")
    ids = _by_id(rep)
    assert "extra-function:custody.trg_CashBalance_Guard_fn" not in ids
    assert ids["trigger-fn:custody.trg_CashBalance_Guard"].target_name == \
        "custody.trg_CashBalance_Guard_fn"
    assert rep.extra == 0


def test_trigger_function_absent_in_target_yields_no_phantom_match():
    # If the trigger function was never created (e.g. the trigger failed to
    # migrate), don't fabricate a matched row for a function that isn't there.
    trg = ProgrammableObject(schema_name="dbo", object_name="trg_Order_Audit",
                             object_type="TRIGGER", line_count=1, definition="...")
    inv = TargetInventory(schemas={"public"})  # no trigger, no function
    ids = _by_id(compare([], [trg], inv))
    assert "trigger-fn:dbo.trg_Order_Audit" not in ids


def test_expected_schemas_scope():
    tables = [_table(), _table(schema="SalesLT", name="Product")]
    assert expected_schemas(tables, [], "app") == ["app", "saleslt"]
    assert expected_schemas([], [], "public") == ["public"]


# --- fetch_target_inventory -----------------------------------------------------------


class FakeTarget:
    database = "tgtdb"

    def query(self, sql, params=None, statement_timeout_ms=None):
        if "schemata" in sql:
            return [{"name": "public"}, {"name": "saleslt"}]
        if "BASE TABLE" in sql:
            return [{"schema": "public", "name": "orders"}]
        if "information_schema.views" in sql:
            return [{"schema": "public", "name": "v_orders"}]
        if "pg_proc" in sql:
            return [{"schema": "public", "name": "usp_do", "kind": "procedure"},
                    {"schema": "public", "name": "fn_x", "kind": "function"}]
        if "information_schema.triggers" in sql:
            return [{"schema": "public", "name": "trg_x"}]
        if "information_schema.columns" in sql:
            return [{"schema": "public", "table": "orders", "name": "Id", "data_type": "integer",
                     "has_default": True, "is_identity": True}]
        if "pg_constraint" in sql and "contype" in sql:
            return [
                {"schema": "public", "table": "orders", "name": "pk_orders",
                 "kind": "p", "columns": ["Id"]},
                {"schema": "public", "table": "orders", "name": "ck_qty",
                 "kind": "c", "columns": ["Qty"]},
                {"schema": "public", "table": "orders", "name": "fk_orders_customer",
                 "kind": "f", "columns": ["CustomerId"]},
            ]
        if "pg_index" in sql:
            return [{"schema": "public", "table": "orders", "name": "orders_ix_total",
                     "is_unique": False, "columns": ["Total"]}]
        if "count(*)" in sql:
            return [{"n": 2}]
        raise AssertionError(f"unexpected SQL: {sql}")

    def execute(self, sql, params=None):
        pass  # ANALYZE — no-op for the fake


def test_fetch_target_inventory_buckets_objects():
    inv = fetch_target_inventory(FakeTarget(), ["public"])
    assert ("public", "orders") in inv.tables
    assert ("public", "v_orders") in inv.views
    assert ("public", "usp_do") in inv.procedures
    assert ("public", "fn_x") in inv.functions
    assert ("public", "trg_x") in inv.triggers
    assert inv.columns[("public", "orders")] == {"Id": "integer"}


def test_routines_query_excludes_event_trigger_and_extension_functions():
    # Platform plumbing (Lakebase's grant_*_on_new_* event-trigger helpers, and
    # extension-owned routines) must be filtered in SQL so they never reach the
    # target inventory as "extra" functions.
    from backend.validation.comparator import _PG_ROUTINES_SQL

    assert "pg_event_trigger" in _PG_ROUTINES_SQL
    assert "d.deptype = 'e'" in _PG_ROUTINES_SQL


# --- run_validation end-to-end (fake connectors) ---------------------------------------


class FakeSource:
    database = "srcdb"

    def query(self, sql, timeout=None):
        if "dm_db_partition_stats" in sql:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "Orders", "ROW_COUNT": 3}]
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "Orders", "COLUMN_NAME": "Id",
                     "DATA_TYPE": "int", "CHARACTER_MAXIMUM_LENGTH": None,
                     "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 0, "IS_NULLABLE": "NO"}]
        if "TABLE_CONSTRAINTS" in sql:
            return []
        # Constraint/index metadata for the plan's post-data phase — none here.
        if any(m in sql for m in ("sys.foreign_keys", "sys.indexes", "sys.default_constraints",
                                  "sys.check_constraints", "sys.identity_columns")):
            return []
        if "sql_modules" in sql:
            return [{"SCHEMA_NAME": "dbo", "OBJECT_NAME": "usp_Report",
                     "OBJECT_TYPE": "SQL_STORED_PROCEDURE",
                     "DEFINITION": "CREATE PROCEDURE dbo.usp_Report AS SELECT 1"}]
        if "COUNT_BIG" in sql:
            return [{"n": 3}]
        raise AssertionError(f"unexpected SQL: {sql}")


def test_run_validation_end_to_end():
    phases = []
    report = run_validation(
        FakeSource(), FakeTarget(), "public",
        progress=lambda phase, done, total, current: phases.append(phase),
    )
    items = _by_id(report)
    # Table exists on both sides but exact counts differ (3 source vs 2 target).
    t = items["table:dbo.Orders"]
    assert t.status is MatchStatus.MISMATCH and t.source_rows == 3 and t.target_rows == 2
    # Procedure exists only in the source.
    assert items["procedure:dbo.usp_Report"].status is MatchStatus.MISSING
    assert report.source_database == "srcdb" and report.target_database == "tgtdb"
    assert report.source_rows == 3 and report.target_rows == 2 and report.tables_compared == 1
    assert phases[0] == "Scanning source" and "Comparing row counts" in phases
    assert phases[-1] == "Building report"


def test_run_validation_objects_scope_skips_tables_and_counts():
    phases = []
    report = run_validation(
        FakeSource(), FakeTarget(), "public", scope="objects",
        progress=lambda phase, done, total, current: phases.append(phase),
    )
    # No table items at all — not compared, and target tables not flagged extra.
    assert not [i for i in report.items if i.kind is ObjectKind.TABLE]
    # No per-table COUNT(*) pass — this is what makes the re-check fast.
    assert "Comparing row counts" not in phases
    # Code objects still compared: the procedure is missing in the target.
    assert _by_id(report)["procedure:dbo.usp_Report"].status is MatchStatus.MISSING


class HugeSource:
    """One table well above the exact-count threshold (600M rows)."""
    database = "srcdb"

    def __init__(self):
        self.count_big_calls = 0
        self.count_timeouts: list[int | None] = []

    def query(self, sql, timeout=None):
        if "dm_db_partition_stats" in sql:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "Position", "ROW_COUNT": 600_000_000}]
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "Position", "COLUMN_NAME": "Id",
                     "DATA_TYPE": "int", "CHARACTER_MAXIMUM_LENGTH": None,
                     "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 0, "IS_NULLABLE": "NO"}]
        if "TABLE_CONSTRAINTS" in sql or any(
            m in sql for m in ("sys.foreign_keys", "sys.indexes", "sys.default_constraints",
                               "sys.check_constraints", "sys.identity_columns")):
            return []
        if "sql_modules" in sql:
            return []
        if "COUNT_BIG" in sql:
            self.count_big_calls += 1
            self.count_timeouts.append(timeout)
            return [{"n": 600_000_000}]
        raise AssertionError(f"unexpected SQL: {sql}")


class HugeTarget:
    database = "tgtdb"

    def __init__(self):
        self.analyze_calls = 0
        self.count_calls = 0
        self.count_timeouts: list[int | None] = []

    def query(self, sql, params=None, statement_timeout_ms=None):
        if "schemata" in sql:
            return [{"name": "public"}]
        if "BASE TABLE" in sql:
            return [{"schema": "public", "name": "position"}]
        if "information_schema.views" in sql or "pg_proc" in sql or \
           "information_schema.triggers" in sql or "pg_constraint" in sql or "pg_index" in sql:
            return []
        if "information_schema.columns" in sql:
            return [{"schema": "public", "table": "position", "name": "Id", "data_type": "integer",
                     "has_default": False, "is_identity": False}]
        if "reltuples" in sql:
            return [{"n": 600_000_000}]
        if "count(*)" in sql:
            self.count_calls += 1
            self.count_timeouts.append(statement_timeout_ms)
            return [{"n": 600_000_000}]
        raise AssertionError(f"unexpected SQL: {sql}")

    def execute(self, sql, params=None):
        assert sql.startswith("ANALYZE"), sql
        self.analyze_calls += 1


def test_huge_table_uses_estimate_by_default():
    src, tgt = HugeSource(), HugeTarget()
    report = run_validation(src, tgt, "public")  # use_estimates defaults True
    t = _by_id(report)["table:dbo.Position"]
    assert t.status is MatchStatus.MATCHED and t.rows_approximate is True
    assert t.source_rows == 600_000_000 and t.target_rows == 600_000_000
    # No exact COUNT(*) ran on either side; the target was ANALYZE'd for reltuples.
    assert src.count_big_calls == 0 and tgt.count_calls == 0 and tgt.analyze_calls == 1
    assert report.tables_estimated == 1


def test_huge_table_exact_mode_counts_both_sides_with_timeout():
    src, tgt = HugeSource(), HugeTarget()
    report = run_validation(src, tgt, "public", use_estimates=False)
    t = _by_id(report)["table:dbo.Position"]
    assert t.status is MatchStatus.MATCHED and t.rows_approximate is False
    assert t.source_rows == 600_000_000 and t.target_rows == 600_000_000
    # Exact COUNT(*) ran on both sides, each with the generous 600s timeout, and
    # no estimate/ANALYZE was used.
    assert src.count_big_calls == 1 and src.count_timeouts == [600]
    assert tgt.count_calls == 1 and tgt.count_timeouts == [600_000]
    assert tgt.analyze_calls == 0
    assert report.tables_estimated == 0 and report.tables_compared == 1


def test_merge_object_rescan_refreshes_objects_and_keeps_tables():
    inv_before = TargetInventory(
        schemas={"public"}, tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer", "Total": "numeric"}},
    )
    previous = compare([_table()], [_proc()], inv_before,
                       source_counts={("dbo", "Orders"): 100},
                       target_counts={("dbo", "Orders"): 97},
                       source_database="srcdb", target_database="tgtdb")
    assert _by_id(previous)["procedure:dbo.usp_Report"].status is MatchStatus.MISSING

    # After the agent created the procedure, the objects-only re-scan sees it.
    inv_after = TargetInventory(schemas={"public"}, procedures={("public", "usp_report")})
    rescan = compare([], [_proc()], inv_after, include_tables=False)

    merged = merge_object_rescan(previous, rescan)
    items = _by_id(merged)
    assert items["procedure:dbo.usp_Report"].status is MatchStatus.MATCHED
    # The table's structure/row results carried over without re-counting.
    t = items["table:dbo.Orders"]
    assert t.status is MatchStatus.MISMATCH and t.source_rows == 100 and t.target_rows == 97
    assert merged.source_rows == 100 and merged.target_rows == 97
    assert merged.missing == 0 and merged.mismatched == 1
    # Score recomputed over the merged items: schema + procedure matched, table
    # mismatched → 2/3 → 66% (floored, so 100 means literally everything matches).
    assert merged.match_score == 66
    assert merged.source_database == "srcdb" and merged.target_database == "tgtdb"


# --- Post-data kinds: constraints, indexes, foreign keys --------------------------------


def _constrained_table(**overrides):
    """A table with one object of every post-data kind."""
    base = dict(
        schema_name="dbo", table_name="Orders", row_count=10, column_count=3,
        columns=[
            ColumnInfo(name="Id", data_type="int", is_nullable=False),
            ColumnInfo(name="Qty", data_type="int"),
            ColumnInfo(name="CustomerId", data_type="int"),
        ],
        primary_key=["Id"],
        check_constraints=[CheckConstraintInfo(name="CK_Qty", definition="([Qty]>(0))")],
        column_defaults=[ColumnDefaultInfo(column="Qty", definition="((1))")],
        indexes=[IndexInfo(name="IX_Qty", columns=[IndexColumnInfo(name="Qty")])],
        foreign_keys=[ForeignKeyInfo(name="FK_Cust", columns=["CustomerId"],
                                     ref_schema="dbo", ref_table="Customer",
                                     ref_columns=["Id"])],
    )
    return TableInfo(**{**base, **overrides})


def _target(**overrides):
    """Target inventory for the table above, with the post-data catalogs read."""
    base = dict(
        schemas={"public"}, tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer", "Qty": "integer",
                                        "CustomerId": "integer"}},
        post_data_scanned=True,
    )
    return TargetInventory(**{**base, **overrides})


_ALL_PRESENT = dict(
    primary_keys={("public", "orders"): TargetObject(name="pk_orders", columns=("Id",))},
    checks={("public", "orders"): [TargetObject(name="ck_qty")]},
    column_defaults={("public", "orders"): {"Qty"}},
    indexes={("public", "orders"): [TargetObject(name="orders_ix_qty", columns=("Qty",))]},
    foreign_keys={("public", "orders"): [TargetObject(name="fk_cust",
                                                      columns=("CustomerId",))]},
)


def test_post_data_kinds_all_present_roll_up_as_matched():
    rep = compare([_constrained_table()], [], _target(**_ALL_PRESENT))
    items = _by_id(rep)
    # One item per kind per table — not one per object.
    c = items["constraint:dbo.Orders"]
    assert c.status is MatchStatus.MATCHED and c.kind is ObjectKind.CONSTRAINT
    assert (c.objects_present, c.objects_expected) == (3, 3)   # PK + check + default
    assert c.detail == "All 3 constraints present."
    assert items["index:dbo.Orders"].status is MatchStatus.MATCHED
    assert items["foreign_key:dbo.Orders"].status is MatchStatus.MATCHED
    assert rep.match_score == 100


def test_missing_foreign_key_names_it_and_generates_the_ddl():
    inv = _target(**{**_ALL_PRESENT, "foreign_keys": {}})
    fk = _by_id(compare([_constrained_table()], [], inv))["foreign_key:dbo.Orders"]
    assert fk.status is MatchStatus.MISMATCH and fk.severity is Severity.HIGH
    assert (fk.objects_present, fk.objects_expected) == (0, 1)
    assert "1 missing in the target" in fk.detail
    # The expanded row names the specific object…
    missing = [o for o in fk.objects if o.status is MatchStatus.MISSING]
    assert [o.name for o in missing] == ["FK_Cust → public.customer (Id)"]
    # …and the deterministic fix recreates exactly it, via the migration's own DDL.
    assert 'ADD CONSTRAINT "fk_cust" FOREIGN KEY ("CustomerId")' in fk.fix_sql
    assert 'REFERENCES "public"."customer" ("Id")' in fk.fix_sql


def test_missing_index_and_check_are_reported_per_kind():
    inv = _target(**{**_ALL_PRESENT, "indexes": {}, "checks": {}})
    items = _by_id(compare([_constrained_table()], [], inv))
    idx = items["index:dbo.Orders"]
    assert idx.status is MatchStatus.MISMATCH and (idx.objects_present, idx.objects_expected) == (0, 1)
    assert 'CREATE INDEX IF NOT EXISTS "orders_ix_qty"' in idx.fix_sql
    # The check lives in the constraint rollup with the PK and default, which are
    # still present — so the count shows partial coverage, not a total failure.
    c = items["constraint:dbo.Orders"]
    assert (c.objects_present, c.objects_expected) == (2, 3)
    missing = [o for o in c.objects if o.status is MatchStatus.MISSING]
    assert [o.name for o in missing] == ["CHECK CK_Qty"]
    # Check predicates are compared by existence, so the source T-SQL is carried
    # for the user to eyeball rather than diffed as text.
    assert missing[0].source_definition == "([Qty]>(0))"
    assert 'ADD CONSTRAINT "ck_qty" CHECK (("Qty">(0)))' in c.fix_sql


def test_primary_key_on_different_columns_is_a_mismatch():
    inv = _target(**{**_ALL_PRESENT,
                     "primary_keys": {("public", "orders"): TargetObject(
                         name="pk_orders", columns=("Id", "Qty"))}})
    c = _by_id(compare([_constrained_table()], [], inv))["constraint:dbo.Orders"]
    assert c.status is MatchStatus.MISMATCH
    drifted = [o for o in c.objects if o.status is MatchStatus.MISMATCH]
    assert "source (Id), target (Id, Qty)" in drifted[0].detail


def test_target_only_index_is_extra_but_not_a_high_severity_gap():
    inv = _target(**{**_ALL_PRESENT,
                     "indexes": {("public", "orders"): [
                         TargetObject(name="orders_ix_qty", columns=("Qty",)),
                         TargetObject(name="orders_ix_adhoc", columns=("Id",)),
                     ]}})
    idx = _by_id(compare([_constrained_table()], [], inv))["index:dbo.Orders"]
    # Everything the source asked for is present, so this is a review item.
    assert idx.status is MatchStatus.MISMATCH and idx.severity is Severity.LOW
    assert (idx.objects_present, idx.objects_expected) == (1, 1)
    assert "1 only in the target" in idx.detail
    extra = [o for o in idx.objects if o.status is MatchStatus.EXTRA]
    assert [o.name for o in extra] == ["orders_ix_adhoc"]
    # Nothing is missing, so there is no deterministic create-fix to offer.
    assert idx.fix_sql == ""


def test_tables_without_post_data_objects_emit_no_rollup_rows():
    """A table with no constraints of a kind must not add an empty "0 of 0" row."""
    plain = TableInfo(schema_name="dbo", table_name="Log", row_count=1, column_count=1,
                      columns=[ColumnInfo(name="Msg", data_type="varchar")])
    inv = _target(tables={("public", "log")},
                  columns={("public", "log"): {"Msg": "character varying"}})
    rep = compare([plain], [], inv)
    assert not [i for i in rep.items if i.kind in
                (ObjectKind.CONSTRAINT, ObjectKind.INDEX, ObjectKind.FOREIGN_KEY)]


def test_unscanned_inventory_does_not_report_constraints_as_missing():
    """post_data_scanned=False means "nobody looked" — not "the target has none".

    Without this, every structure-only compare would report a correctly-migrated
    database's constraints as missing.
    """
    rep = compare([_constrained_table()], [], _target(post_data_scanned=False))
    assert not [i for i in rep.items if i.kind in
                (ObjectKind.CONSTRAINT, ObjectKind.INDEX, ObjectKind.FOREIGN_KEY)]


def test_missing_table_does_not_also_report_its_constraints():
    """The missing table is the finding; repeating it per kind is noise."""
    rep = compare([_constrained_table()], [], _target(tables=set(), columns={}))
    assert _by_id(rep)["table:dbo.Orders"].status is MatchStatus.MISSING
    assert not [i for i in rep.items if i.kind in
                (ObjectKind.CONSTRAINT, ObjectKind.INDEX, ObjectKind.FOREIGN_KEY)]


def test_post_data_names_follow_the_identifier_case_policy():
    """The comparator must look for the names ddl_generator actually creates."""
    inv = TargetInventory(
        schemas={"public"}, tables={("public", "Orders")},
        columns={("public", "Orders"): {"Id": "integer", "Qty": "integer",
                                        "CustomerId": "integer"}},
        post_data_scanned=True,
        primary_keys={("public", "Orders"): TargetObject(name="pk_Orders", columns=("Id",))},
        checks={("public", "Orders"): [TargetObject(name="CK_Qty")]},
        column_defaults={("public", "Orders"): {"Qty"}},
        indexes={("public", "Orders"): [TargetObject(name="Orders_IX_Qty", columns=("Qty",))]},
        foreign_keys={("public", "Orders"): [TargetObject(name="FK_Cust",
                                                          columns=("CustomerId",))]},
    )
    items = _by_id(compare([_constrained_table()], [], inv, identifier_case="preserve"))
    assert items["constraint:dbo.Orders"].status is MatchStatus.MATCHED
    assert items["index:dbo.Orders"].status is MatchStatus.MATCHED
    assert items["foreign_key:dbo.Orders"].status is MatchStatus.MATCHED


def test_objects_rescan_rechecks_post_data_and_keeps_table_rows():
    """The agent fixes constraints, so the fast re-scan must re-check them —
    a carried-over copy would keep reporting a created FK as missing."""
    before = _target(**{**_ALL_PRESENT, "foreign_keys": {}})
    previous = compare([_constrained_table()], [], before,
                       source_counts={("dbo", "Orders"): 10},
                       target_counts={("dbo", "Orders"): 10})
    assert _by_id(previous)["foreign_key:dbo.Orders"].status is MatchStatus.MISMATCH

    # Agent applied the FK; the objects-scope re-scan sees it.
    rescan = compare([_constrained_table()], [], _target(**_ALL_PRESENT), include_tables=False)
    merged = merge_object_rescan(previous, rescan)
    items = _by_id(merged)
    assert items["foreign_key:dbo.Orders"].status is MatchStatus.MATCHED
    # The table item carried over — no re-counting — and no duplicate was added.
    t = items["table:dbo.Orders"]
    assert t.source_rows == 10 and t.target_rows == 10
    assert sum(1 for i in merged.items if i.kind is ObjectKind.TABLE) == 1
    assert merged.match_score == 100


def test_compare_without_tables_flags_no_extra_tables():
    inv = TargetInventory(schemas={"public"}, tables={("public", "zombie")})
    report = compare([], [_proc()], inv, include_tables=False)
    assert not [i for i in report.items if i.kind is ObjectKind.TABLE]


def test_merge_object_rescan_keeps_table_only_schemas():
    # "SalesLT" hosts only tables, so the objects re-scan can't see it — its
    # schema item must carry over from the previous report instead of vanishing.
    inv_before = TargetInventory(schemas={"public"})
    previous = compare([_table(schema="SalesLT", name="Product")], [_proc()], inv_before)
    assert _by_id(previous)["schema:SalesLT"].status is MatchStatus.MISSING

    inv_after = TargetInventory(schemas={"public"}, procedures={("public", "usp_report")})
    rescan = compare([], [_proc()], inv_after, include_tables=False)

    items = _by_id(merge_object_rescan(previous, rescan))
    assert items["schema:SalesLT"].status is MatchStatus.MISSING   # carried over
    assert items["schema:dbo"].status is MatchStatus.MATCHED       # re-checked fresh
    assert items["procedure:dbo.usp_Report"].status is MatchStatus.MATCHED


# --- AI fixer ---------------------------------------------------------------------------


def _item(**overrides):
    base = dict(
        id="procedure:dbo.usp_Report", kind=ObjectKind.PROCEDURE,
        source_name="dbo.usp_Report", target_name="public.usp_report",
        status=MatchStatus.MISSING, detail="Procedure missing.",
        source_definition="CREATE PROCEDURE dbo.usp_Report AS SELECT 1",
    )
    base.update(overrides)
    return ValidationItem(**base)


def test_parse_proposal_handles_json_fences_and_prose_fallback():
    payload = {"analysis": "translate to plpgsql", "sql": "CREATE PROCEDURE public.usp_report() ..."}
    assert fixer._parse_proposal(json.dumps(payload)) == payload
    assert fixer._parse_proposal(f"Sure:\n```json\n{json.dumps(payload)}\n```") == payload
    out = fixer._parse_proposal("DROP TABLE t;")
    assert out["sql"] == "DROP TABLE t;" and out["analysis"]


def test_parse_proposal_tolerates_raw_newlines_inside_strings():
    # Models pretty-print the JSON with real line breaks inside the "sql" string —
    # invalid strict JSON that used to fall back to dumping the blob verbatim.
    content = '{\n  "analysis": "missing procedure",\n  "sql": "\nCREATE PROCEDURE market.usp_dailyclose()\nLANGUAGE plpgsql\nAS $$\nBEGIN\nEND;\n$$;"\n}'
    out = fixer._parse_proposal(content)
    assert out is not None and out["analysis"] == "missing procedure"
    assert "CREATE PROCEDURE market.usp_dailyclose()" in out["sql"]


def test_parse_proposal_rejects_truncated_json():
    truncated = '{\n  "analysis": "The procedure is missing...",\n  "sql": "\nCREATE OR REPLACE PROCEDURE market.usp_dailyclose(\n    p_close_date DATE\n)\nLANGUAGE plpgsql\nAS $$\nDECLARE'
    assert fixer._parse_proposal(truncated) is None


def test_fix_prompt_carries_discrepancy_context():
    item = _item(columns_missing=["Total"], type_drift=["Id: expected integer, found text"],
                 fix_sql="ALTER TABLE ...")
    prompt = fixer._build_user_prompt(item, "public")
    assert "public.usp_report" in prompt and "dbo.usp_Report" in prompt
    assert "Total" in prompt and "found text" in prompt
    assert "CREATE PROCEDURE dbo.usp_Report" in prompt
    assert "ALTER TABLE ..." in prompt


def test_clean_sql_strips_markdown_fences():
    assert fixer._clean_sql("```sql\nSELECT 1;\n```") == "SELECT 1;"
    assert fixer._clean_sql("```\nSELECT 1;\n```") == "SELECT 1;"


def test_clean_sql_recovers_double_escaped_newlines():
    raw = "CREATE VIEW public.v AS\\nSELECT a\\nFROM public.t;"  # literal backslash-n
    assert fixer._clean_sql(raw) == "CREATE VIEW public.v AS\nSELECT a\nFROM public.t;"


def test_reflow_breaks_single_line_sql_at_clauses():
    raw = ("CREATE OR REPLACE VIEW public.v_active AS SELECT Id, Name, Total "
           "FROM public.customers WHERE Active = true ORDER BY Name")
    out = fixer._clean_sql(raw)
    assert out.splitlines() == [
        "CREATE OR REPLACE VIEW public.v_active AS",
        "SELECT Id, Name, Total",
        "FROM public.customers",
        "WHERE Active = true",
        "ORDER BY Name",
    ]


def test_reflow_respects_quoted_strings_and_leaves_formatted_sql_alone():
    raw = ("INSERT INTO public.log (msg, src) VALUES ('rows FROM t WHERE x', 'etl') "
           "RETURNING id, msg, src, created_at")
    out = fixer._clean_sql(raw)
    assert "'rows FROM t WHERE x'" in out          # string literal untouched
    assert "\nRETURNING id" in out                 # clause outside quotes broken
    formatted = "SELECT a\nFROM t\nWHERE a > 1\nORDER BY a"
    assert fixer._clean_sql(formatted) == formatted


def test_reflow_formats_dollar_quoted_bodies_and_statements():
    raw = ("CREATE FUNCTION public.trg_fn() RETURNS trigger LANGUAGE plpgsql AS "
           "$$ BEGIN UPDATE public.t SET x = 1 WHERE id = NEW.id; RETURN NEW; END $$; "
           "CREATE TRIGGER trg AFTER INSERT ON public.t FOR EACH ROW EXECUTE FUNCTION public.trg_fn();")
    out = fixer._clean_sql(raw)
    assert "\nLANGUAGE plpgsql" in out             # outer clause broken
    assert "\nWHERE id = NEW.id" in out            # inside the $$ body too
    assert ";\n\nCREATE TRIGGER" in out            # statements separated


def _resp(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason,
    )])


def test_propose_fix_handles_structured_content_blocks(monkeypatch):
    # Reasoning models (e.g. fable-5) return message.content as a list of
    # blocks — reasoning first, then the text block(s) with the answer.
    blocks = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking…"}]},
        {"type": "text", "text": json.dumps({"analysis": "ok", "sql": "SELECT 1;"})},
    ]
    monkeypatch.setattr(fixer, "query_chat", lambda endpoint, messages, **p: _resp(blocks))
    out = fixer.propose_fix(_item(), "public")
    assert out.success and out.sql == "SELECT 1;" and out.analysis == "ok"


def test_propose_fix_cleans_the_sql(monkeypatch):
    dirty = {"analysis": "ok", "sql": "```sql\nSELECT 1;\n```"}
    monkeypatch.setattr(fixer, "query_chat", lambda endpoint, messages, **p: _resp(json.dumps(dirty)))
    out = fixer.propose_fix(_item(), "public")
    assert out.success and out.sql == "SELECT 1;"


def test_propose_fix_success_and_requests_structured_output(monkeypatch):
    sent = {}

    def fake(endpoint, messages, **params):
        sent.update(params)
        return _resp(json.dumps({"analysis": "ok", "sql": "SELECT 1"}))

    monkeypatch.setattr(fixer, "query_chat", fake)
    out = fixer.propose_fix(_item(), "public", endpoint="my-endpoint")
    assert out.success and out.sql == "SELECT 1" and out.endpoint == "my-endpoint"
    # Structured output constrains the reply to {"analysis", "sql"}.
    assert sent["response_format"]["type"] == "json_schema"
    schema = sent["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"analysis", "sql"}
    # Long procedure bodies must fit — 4000 used to truncate mid-"sql".
    assert sent["max_tokens"] >= 8000


def test_propose_fix_retries_once_when_sql_left_empty(monkeypatch):
    # The failure seen in the field: valid structured JSON whose "sql" is blank
    # for a missing procedure. The fixer pushes back once and takes the retry.
    replies = [
        _resp(json.dumps({"analysis": "will create the procedure", "sql": " "})),
        _resp(json.dumps({"analysis": "created", "sql": "CREATE PROCEDURE public.usp_report()\nLANGUAGE plpgsql\nAS $$\nBEGIN\nEND;\n$$;"})),
    ]
    calls = []

    def fake(endpoint, messages, **params):
        calls.append(messages)
        return replies[len(calls) - 1]

    monkeypatch.setattr(fixer, "query_chat", fake)
    out = fixer.propose_fix(_item(), "public")
    assert out.success and "CREATE PROCEDURE public.usp_report()" in out.sql
    assert len(calls) == 2
    # The retry carries the empty reply plus the corrective instruction.
    assert calls[1][-1].content == fixer._RETRY_PROMPT
    assert "will create the procedure" in calls[1][-2].content


def test_propose_fix_fails_clearly_when_model_never_returns_sql(monkeypatch):
    calls = []

    def fake(endpoint, messages, **params):
        calls.append(1)
        return _resp(json.dumps({"analysis": "explains the fix but returns nothing", "sql": ""}))

    monkeypatch.setattr(fixer, "query_chat", fake)
    out = fixer.propose_fix(_item(), "public")
    assert not out.success and "no SQL" in (out.error or "")
    assert len(calls) == 2  # exactly one push-back, then give up


def test_propose_fix_accepts_empty_sql_for_row_count_mismatch(monkeypatch):
    calls = []

    def fake(endpoint, messages, **params):
        calls.append(1)
        return _resp(json.dumps({"analysis": "re-copy the table data", "sql": ""}))

    monkeypatch.setattr(fixer, "query_chat", fake)
    item = _item(id="table:dbo.Orders", kind=ObjectKind.TABLE, source_name="dbo.Orders",
                 target_name="public.orders", status=MatchStatus.MISMATCH,
                 detail="Row counts differ.", source_definition="",
                 source_rows=100, target_rows=90)
    out = fixer.propose_fix(item, "public")
    assert out.success and out.sql == "" and "re-copy" in out.analysis
    assert len(calls) == 1  # no push-back — empty SQL is the right answer here


def test_propose_fix_reports_token_limit_truncation(monkeypatch):
    monkeypatch.setattr(fixer, "query_chat",
                        lambda endpoint, messages, **p: _resp('{"analysis": "x", "sql": "CREATE', "length"))
    out = fixer.propose_fix(_item(), "public")
    assert not out.success and "token limit" in (out.error or "")


def test_propose_fix_reports_malformed_json_instead_of_dumping_it(monkeypatch):
    monkeypatch.setattr(fixer, "query_chat",
                        lambda endpoint, messages, **p: _resp('{"analysis": "x", "sql": "CREATE'))
    out = fixer.propose_fix(_item(), "public")
    assert not out.success and "malformed" in (out.error or "")


def test_propose_fix_is_fail_soft(monkeypatch):
    def boom(endpoint, messages, **params):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(fixer, "query_chat", boom)
    out = fixer.propose_fix(_item(), "public")
    assert not out.success and "endpoint down" in (out.error or "")


# --- Run registry -------------------------------------------------------------------------


def _run_request():
    return ValidationRunRequest(
        source=ConnectionRequest(host="h", database="d", username="u", password="p"),
        lakebase=LakebaseConnRequest(host="h", database="d", user="u", password="p"),
    )


def _execute_inline(req):
    run_id = "test-run"
    with runs._LOCK:
        runs._RUNS[run_id] = ValidationRunState(run_id=run_id)
    runs._execute(run_id, req)
    return runs.get_run(run_id)


def test_run_success_stores_report(monkeypatch):
    remembered = {}
    scopes = []
    report = ValidationReport(source_database="s", target_database="t", target_schema="public")

    def fake_run(src, tgt, schema, progress, scope="full", use_estimates=True):
        scopes.append(scope)
        return report

    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: SimpleNamespace(database="t"))
    monkeypatch.setattr(runs, "run_validation", fake_run)
    monkeypatch.setattr(runs, "remember_effective", lambda *a: remembered.setdefault("called", True))

    state = _execute_inline(_run_request())
    assert state.status == "success" and state.phase == "done"
    assert state.report is not None and state.report.source_database == "s"
    assert scopes == ["full"]
    assert remembered["called"]


def test_objects_scope_run_merges_into_the_previous_report(monkeypatch):
    previous = ValidationReport(
        source_database="s", target_database="t", target_schema="public",
        items=[ValidationItem(id="table:dbo.Orders", kind=ObjectKind.TABLE,
                              source_name="dbo.Orders", target_name="public.orders",
                              status=MatchStatus.MATCHED, source_rows=100, target_rows=100)],
    )
    rescan = ValidationReport(
        source_database="s", target_database="t", target_schema="public",
        items=[ValidationItem(id="procedure:dbo.usp_Report", kind=ObjectKind.PROCEDURE,
                              source_name="dbo.usp_Report", target_name="public.usp_report",
                              status=MatchStatus.MATCHED)],
    )
    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: SimpleNamespace(database="t"))
    monkeypatch.setattr(runs, "run_validation",
                        lambda src, tgt, schema, progress, scope="full", use_estimates=True: rescan)
    monkeypatch.setattr(runs, "remember_effective", lambda *a: None)

    req = _run_request().model_copy(update={"scope": "objects", "previous": previous})
    state = _execute_inline(req)
    ids = {i.id for i in state.report.items}
    # The stored report contains the re-scanned procedure AND the carried-over table.
    assert ids == {"table:dbo.Orders", "procedure:dbo.usp_Report"}
    assert state.report.source_rows == 100 and state.report.matched == 2


def test_run_failure_surfaces_error(monkeypatch):
    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: object())

    def boom(src, tgt, schema, progress, scope="full", use_estimates=True):
        raise RuntimeError("target unreachable")
    monkeypatch.setattr(runs, "run_validation", boom)

    state = _execute_inline(_run_request())
    assert state.status == "failed" and "target unreachable" in (state.error or "")
