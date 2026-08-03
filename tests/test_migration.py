"""Engine logic that runs without a live database."""
import time

from backend.assessment.models import ColumnInfo, ProgrammableObject, TableInfo
from backend.migration import plan_runs
from backend.migration.data_loader import _bit_indices, _source_select
from backend.migration.executor import _ordered
from backend.migration.models import BuildPlanRequest, ObjectKind
from backend.migration.planner import build_plan
from backend.schema_migration.naming import (
    IdentifierCase,
    map_object,
    map_schema,
    trigger_function_name,
)


def _table():
    return TableInfo(
        schema_name="dbo", table_name="Orders", row_count=3, column_count=2,
        columns=[ColumnInfo(name="Id", data_type="int", is_nullable=False),
                 ColumnInfo(name="Active", data_type="bit")],
    )


def test_plan_has_schema_table_and_code_items():
    objs = [ProgrammableObject(schema_name="dbo", object_name="vw", object_type="VIEW", line_count=1, definition="SELECT 1")]
    items = build_plan([_table()], objs, "public", translate=False, endpoint=None)
    kinds = [i.kind for i in items]
    assert kinds[0] is ObjectKind.SCHEMA
    assert ObjectKind.TABLE in kinds and ObjectKind.VIEW in kinds
    # Code item without translation has empty sql (must not be applied blindly).
    view = next(i for i in items if i.kind is ObjectKind.VIEW)
    assert view.sql == "" and view.original == "SELECT 1"


def test_apply_order_respects_dependencies():
    items = build_plan(
        [_table()],
        [
            ProgrammableObject(schema_name="dbo", object_name="trg", object_type="TRIGGER", line_count=1, definition="x"),
            ProgrammableObject(schema_name="dbo", object_name="fn", object_type="FUNCTION", line_count=1, definition="x"),
        ],
        "public", translate=False, endpoint=None,
    )
    order = [i.kind for i in _ordered(items)]
    assert order.index(ObjectKind.SCHEMA) < order.index(ObjectKind.TABLE)
    assert order.index(ObjectKind.FUNCTION) < order.index(ObjectKind.TRIGGER)


def test_bit_columns_detected_for_coercion():
    assert _bit_indices(_table().columns, ["Id", "Active"]) == {1}


def test_source_select_casts_driver_hostile_types():
    """sql_variant / hierarchyid / spatial columns break pymssql (and Spark) when
    fetched raw — the loader must convert them to text server-side, aliased back
    to the original names so the COPY column list keeps matching."""
    cols = [
        ColumnInfo(name="Id", data_type="int"),
        ColumnInfo(name="Payload", data_type="sql_variant"),
        ColumnInfo(name="Node", data_type="hierarchyid"),
        ColumnInfo(name="Pin", data_type="geography"),
    ]
    assert _source_select(cols) == (
        "[Id], CAST([Payload] AS NVARCHAR(MAX)) AS [Payload], "
        "[Node].ToString() AS [Node], [Pin].STAsText() AS [Pin]"
    )


def test_source_select_falls_back_to_star_without_metadata():
    assert _source_select([]) == "*"


def test_schema_name_mapping():
    assert map_schema("dbo") == "public"          # default schema -> public
    assert map_schema("dbo", "core") == "core"    # configurable dbo target
    assert map_schema("SalesLT") == "saleslt"     # other schemas lower-cased, preserved
    assert map_object("ErrorLog") == "errorlog"


def test_source_case_can_be_preserved():
    assert map_schema("dbo", "AppCore", IdentifierCase.PRESERVE) == "AppCore"
    assert map_schema("SalesLT", "public", IdentifierCase.PRESERVE) == "SalesLT"
    assert map_object("ErrorLog", IdentifierCase.PRESERVE) == "ErrorLog"


def test_trigger_function_name_follows_object_case():
    assert trigger_function_name("trg_Order_Audit") == "trg_order_audit_fn"
    assert trigger_function_name("trg_Order_Audit", IdentifierCase.PRESERVE) == "trg_Order_Audit_fn"


def test_source_schemas_preserved_in_plan():
    tables = [
        TableInfo(schema_name="dbo", table_name="ErrorLog", row_count=0, column_count=1,
                  columns=[ColumnInfo(name="Id", data_type="int")]),
        TableInfo(schema_name="SalesLT", table_name="Product", row_count=0, column_count=1,
                  columns=[ColumnInfo(name="Id", data_type="int")]),
    ]
    items = build_plan(tables, [], "public", translate=False, endpoint=None)
    # One Postgres schema per distinct source schema (dbo -> public, SalesLT -> saleslt).
    schema_items = {i.name for i in items if i.kind is ObjectKind.SCHEMA}
    assert schema_items == {"public", "saleslt"}
    table_items = {i.name for i in items if i.kind is ObjectKind.TABLE}
    assert table_items == {"public.errorlog", "saleslt.product"}
    prod = next(i for i in items if i.name == "saleslt.product")
    assert '"saleslt"."product"' in prod.sql


def test_plan_preserves_case_when_requested():
    table = TableInfo(
        schema_name="SalesLT", table_name="ProductModel", row_count=0, column_count=1,
        columns=[ColumnInfo(name="ProductId", data_type="int")],
    )
    obj = ProgrammableObject(
        schema_name="SalesLT", object_name="GetProducts", object_type="PROCEDURE",
        line_count=1, definition="x",
    )
    items = build_plan(
        [table], [obj], "public", translate=False, endpoint=None,
        identifier_case=IdentifierCase.PRESERVE,
    )
    assert {i.name for i in items if i.kind is ObjectKind.SCHEMA} == {"SalesLT"}
    target = next(i for i in items if i.kind is ObjectKind.TABLE)
    assert target.name == "SalesLT.ProductModel"
    assert '"SalesLT"."ProductModel"' in target.sql
    assert next(i for i in items if i.kind is ObjectKind.PROCEDURE).name == "SalesLT.GetProducts"


# --- Background plan run (start + poll) --------------------------------------------------


def _await_run(run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = plan_runs.get_run(run_id)
        if state and state.status != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("plan run did not finish in time")


def test_plan_run_completes_and_returns_items(monkeypatch):
    # Avoid real FM calls: translate deterministically.
    from backend.migration import planner
    from backend.schema_migration.models import Translation

    def fake_translate_all(objects, endpoint=None, schema_map=None, on_done=None):
        out = []
        for i, o in enumerate(objects, 1):
            out.append(Translation(object_name=f"{o.schema_name}.{o.object_name}",
                                   object_type=o.object_type, original=o.definition,
                                   translated="SELECT 1", reasoning="r", notes="n", success=True))
            if on_done:
                on_done(i, len(objects))
        return out

    monkeypatch.setattr(planner, "translate_all", fake_translate_all)
    objs = [ProgrammableObject(schema_name="dbo", object_name="vw", object_type="VIEW",
                               line_count=1, definition="SELECT 1")]
    req = BuildPlanRequest(tables=[_table()], programmable_objects=objs, target_schema="public")

    run_id = plan_runs.start_run(req)
    state = _await_run(run_id)
    assert state.status == "success" and state.items is not None
    assert state.objects_done == 1 and state.objects_total == 1
    kinds = [i.kind for i in state.items]
    assert ObjectKind.SCHEMA in kinds and ObjectKind.TABLE in kinds and ObjectKind.VIEW in kinds


def test_plan_run_surfaces_failure(monkeypatch):
    from backend.migration import planner

    def boom(*a, **k):
        raise RuntimeError("endpoint exploded")

    monkeypatch.setattr(planner, "translate_all", boom)
    req = BuildPlanRequest(
        tables=[_table()],
        programmable_objects=[ProgrammableObject(schema_name="dbo", object_name="vw",
                                                 object_type="VIEW", line_count=1, definition="x")],
        target_schema="public",
    )
    state = _await_run(plan_runs.start_run(req))
    assert state.status == "failed" and "endpoint exploded" in (state.error or "")


def test_unknown_plan_run_is_none():
    assert plan_runs.get_run("nope") is None
