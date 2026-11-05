"""The source-dialect seam: registry, T-SQL parity, and an engine that calls only
the dialect it is given."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import migration_routes, schema_routes
from backend.assessment.models import (
    CheckConstraintInfo,
    ColumnDefaultInfo,
    ColumnInfo,
    IndexColumnInfo,
    IndexInfo,
    TableInfo,
)
from backend.connectors import factory
from backend.dialects import DEFAULT_DIALECT, SourceDialect, get_dialect, supported_source_types
from backend.dialects.tsql import TSQL, TsqlDialect
from backend.migration.planner import build_plan
from backend.schema_migration import collation_mapper, expr_mapper, pg_collation, type_mapper
from backend.schema_migration.ddl_generator import generate_ddl
from backend.validation.comparator import TargetInventory, compare

CI_AS = "SQL_Latin1_General_CP1_CI_AS"


def _table() -> TableInfo:
    return TableInfo(
        schema_name="dbo", table_name="Orders", row_count=1, column_count=3,
        columns=[
            ColumnInfo(name="Id", data_type="int", is_nullable=False),
            ColumnInfo(name="Name", data_type="nvarchar", max_length=50, collation_name=CI_AS),
            ColumnInfo(name="Active", data_type="bit"),
        ],
        column_defaults=[ColumnDefaultInfo(column="Active", definition="((1))")],
        check_constraints=[CheckConstraintInfo(name="CK_Active", definition="([Active]=(1))")],
        indexes=[IndexInfo(name="IX_Name", columns=[IndexColumnInfo(name="Name")],
                           filter_definition="([Active]=(1))")],
    )


class _FakeDialect(SourceDialect):
    """A non-T-SQL dialect whose every answer is recognisable in the output."""

    name = "fake"

    def map_type(self, col) -> str:
        return f"fake_{col.data_type}"

    def required_extensions(self, tables) -> list[str]:
        return ["fake_ext"]

    def map_expression(self, expr, *, columns=None) -> str:
        return "fake_predicate"

    def map_default_expression(self, expr, *, column=None, target_schema="public",
                               identifier_case="lowercase") -> str:
        return "fake_default"

    def sequence_ref_in(self, expr, target_schema="public", identifier_case="lowercase"):
        return None

    def column_collation(self, col):
        if col.collation_name is None:
            return None
        return pg_collation.TargetCollation(name="fake_coll", locale="und-u-ks-level2",
                                            deterministic=False)


# --- Registry ------------------------------------------------------------------


@pytest.mark.parametrize("source_type", ["azure-sql", "sql-server", " Azure-SQL "])
def test_tsql_sources_resolve_to_the_tsql_dialect(source_type):
    assert get_dialect(source_type) is TSQL


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="not supported"):
        get_dialect("oracle")


def test_every_connector_source_has_a_dialect():
    # The factory and the dialect registry are keyed by the same ids; a connector
    # without a dialect could scan but never generate a plan.
    assert factory._TSQL_SOURCES <= supported_source_types()


def test_default_dialect_is_tsql():
    assert DEFAULT_DIALECT is TSQL


# --- T-SQL delegates to the mappers ---------------------------------------------


def test_tsql_dialect_matches_the_mappers():
    t = _table()
    for col in t.columns:
        assert TSQL.map_type(col) == type_mapper.map_type(col)
        assert TSQL.column_collation(col) == collation_mapper.column_collation(col)
    assert TSQL.required_extensions([t]) == type_mapper.required_extensions([t])
    expr = "([Active]=(1) AND [Name]<>N'x')"
    assert TSQL.map_expression(expr, columns=t.columns) == expr_mapper.map_expression(
        expr, columns=t.columns)
    assert TSQL.map_default_expression("(getdate())") == expr_mapper.map_default_expression(
        "(getdate())")
    assert TSQL.sequence_ref_in("(NEXT VALUE FOR [dbo].[Seq])") == ("public", "seq")
    assert TSQL.collect_collations([t]).columns == collation_mapper.collect_collations(
        [t]).columns


def test_collation_types_are_shared_not_copied():
    # T-SQL's collation module re-exports the Postgres-side types other dialects use.
    assert collation_mapper.TargetCollation is pg_collation.TargetCollation
    assert collation_mapper.SourceCollation is pg_collation.SourceCollation


# --- The engine calls only the dialect it is given ------------------------------


def test_generate_ddl_uses_the_given_dialect():
    script, _ = generate_ddl([_table()], dialect=_FakeDialect())
    assert "CREATE EXTENSION IF NOT EXISTS fake_ext;" in script
    assert '"Id" fake_int NOT NULL' in script
    assert '"Name" fake_nvarchar COLLATE "public"."fake_coll"' in script
    assert "SET DEFAULT fake_default;" in script
    assert "CHECK (fake_predicate)" in script
    assert "WHERE fake_predicate;" in script
    # No T-SQL translation leaked through.
    assert "boolean" not in script and "sql_latin1" not in script


def test_build_plan_uses_the_given_dialect():
    items = build_plan([_table()], [], "public", translate=False, endpoint=None,
                       dialect=_FakeDialect())
    sql = {i.id: i.sql for i in items}
    assert "extension:fake_ext" in sql
    assert "collation:fake_coll" in sql
    assert "fake_int" in sql["table:dbo.Orders"]
    assert "fake_default" in sql["default:dbo.Orders.Active"]
    assert "fake_predicate" in sql["check:dbo.Orders.CK_Active"]


def test_compare_expects_the_given_dialects_types():
    t = _table()
    inv = TargetInventory(
        schemas={"public"},
        tables={("public", "orders")},
        columns={("public", "orders"): {"Id": "integer", "Name": "character varying",
                                         "Active": "boolean"}},
    )
    report = compare([t], [], inv, dialect=_FakeDialect())
    item = next(i for i in report.items if i.id == "table:dbo.Orders")
    assert "expected fake_int" in item.detail


def test_tsql_default_keeps_existing_output():
    t = _table()
    assert generate_ddl([t]) == generate_ddl([t], dialect=TsqlDialect())


# --- Routes resolve the dialect from source_type --------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(schema_routes.router)
    app.include_router(migration_routes.router)
    return TestClient(app)


def test_ddl_route_rejects_an_unsupported_source(client):
    resp = client.post("/api/schema/ddl", json={"tables": [], "source_type": "oracle"})
    assert resp.status_code == 400 and "not supported" in resp.json()["detail"]


def test_plan_routes_reject_an_unsupported_source(client):
    body = {"tables": [], "translate": False, "source_type": "oracle"}
    for path in ("/api/migration/plan", "/api/migration/plan/start"):
        resp = client.post(path, json=body)
        assert resp.status_code == 400, path


def test_plan_route_defaults_to_tsql(client):
    t = _table().model_dump()
    resp = client.post("/api/migration/plan", json={"tables": [t], "translate": False})
    assert resp.status_code == 200
    table_sql = next(i["sql"] for i in resp.json()["items"] if i["id"] == "table:dbo.Orders")
    assert '"Active" boolean' in table_sql
