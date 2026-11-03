"""SQL Server edge types: spatial -> PostGIS, hierarchyid -> ltree, sql_variant ->
jsonb, CLR UDTs -> text — the mapping, the extensions it needs, and the source casts
that produce each type's input form."""
import pglast
import pytest

from backend.assessment import scanner
from backend.assessment.compatibility import check_full_globes
from backend.assessment.models import ColumnInfo, Severity, TableInfo
from backend.migration.data_loader import _check_full_globes
from backend.migration.executor import _ordered
from backend.migration.models import ObjectKind, TableLoadSpec
from backend.migration.planner import build_plan
from backend.migration.source_casts import (
    SOURCE_CASTS,
    TableWarning,
    cast_template,
    full_globe_note,
    full_globe_sql,
)
from backend.schema_migration.ddl_generator import generate_ddl
from backend.schema_migration.type_mapper import map_type, required_extensions
from backend.validation.comparator import _expected_pg_type


def _table(*cols: ColumnInfo, name: str = "Places") -> TableInfo:
    return TableInfo(schema_name="dbo", table_name=name, row_count=1,
                     column_count=len(cols), columns=list(cols))


def _col(data_type: str, name: str = "c", clr: bool = False) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_clr_type=clr)


def test_edge_types_map_to_their_postgres_counterparts():
    assert map_type(_col("geometry")) == "geometry"
    assert map_type(_col("geography")) == "geography"
    assert map_type(_col("hierarchyid")) == "ltree"
    assert map_type(_col("sql_variant")) == "jsonb"
    # A user CLR type has no counterpart; it lands as its ToString() text.
    assert map_type(_col("MyPoint", clr=True)) == "text"


def test_extensions_follow_the_columns_that_need_them():
    assert required_extensions([_table(_col("int"))]) == []
    assert required_extensions([
        _table(_col("geography", "a"), _col("geometry", "b")),
        _table(_col("hierarchyid"), name="Org"),
    ]) == ["ltree", "postgis"]


def test_the_plan_creates_extensions_before_anything_else():
    items = _ordered(build_plan(
        [_table(_col("int", "Id"), _col("geography", "Pin"), _col("hierarchyid", "Node"))],
        [], "public", translate=False, endpoint=None,
    ))
    assert [i.kind for i in items[:2]] == [ObjectKind.EXTENSION, ObjectKind.EXTENSION]
    assert [i.sql for i in items[:2]] == [
        "CREATE EXTENSION IF NOT EXISTS ltree;",
        "CREATE EXTENSION IF NOT EXISTS postgis;",
    ]
    table = next(i for i in items if i.kind is ObjectKind.TABLE)
    assert '"Pin" geography' in table.sql and '"Node" ltree' in table.sql


def test_plans_without_edge_types_get_no_extension_item():
    items = build_plan([_table(_col("int", "Id"))], [], "public", translate=False, endpoint=None)
    assert not any(i.kind is ObjectKind.EXTENSION for i in items)


def test_the_ddl_script_creates_extensions_first_and_parses():
    script, count = generate_ddl([_table(_col("geometry", "Shape"), _col("sql_variant", "V"))])
    assert script.index("CREATE EXTENSION IF NOT EXISTS postgis;") < script.index("CREATE SCHEMA")
    assert count == 3  # extension + schema + table
    pglast.parse_sql(script)


def test_validation_expects_the_extension_type_names():
    # The comparator reads udt_name for USER-DEFINED columns, i.e. these names.
    assert _expected_pg_type(_col("geography")) == "geography"
    assert _expected_pg_type(_col("hierarchyid")) == "ltree"
    assert _expected_pg_type(_col("sql_variant")) == "jsonb"


def test_sql_variant_is_read_as_a_typed_json_object():
    expr = cast_template("sql_variant").format(c="[V]", a="[V]")
    assert expr.startswith("CASE WHEN [V] IS NULL THEN NULL ELSE N'{\"type\":\"' + ")
    assert "SQL_VARIANT_PROPERTY([V], 'BaseType')" in expr
    assert "STRING_ESCAPE(" in expr and expr.endswith("+ N'\"}' END AS [V]")
    # Exact styles where the default string conversion rounds.
    assert "CONVERT(NVARCHAR(MAX), CAST([V] AS FLOAT), 3)" in expr
    assert "CONVERT(NVARCHAR(MAX), CAST([V] AS MONEY), 2)" in expr
    # Seen live: a datetime2 variant cast straight to NVARCHAR gave 'Jan  2 2024  3:04AM'.
    assert "WHEN 'datetime2' THEN CONVERT(NVARCHAR(MAX), CAST([V] AS DATETIME2), 126)" in expr
    assert "WHEN 'time' THEN CAST(CAST([V] AS TIME) AS NVARCHAR(MAX))" in expr


def test_geometry_is_read_as_srid_prefixed_hex_wkb():
    expr = cast_template("geometry").format(c="[S]", a="[S]")
    assert expr == ("'SRID=' + CAST([S].STSrid AS VARCHAR(11)) + ';' + "
                    "CONVERT(VARCHAR(MAX), [S].AsBinaryZM(), 2) AS [S]")


def test_known_types_win_over_the_clr_flag():
    # sys.types marks geometry/hierarchyid as assembly types too.
    assert cast_template("geometry", is_clr_type=True) == SOURCE_CASTS["geometry"]
    assert cast_template("int") is None


class _ScanConn:
    database = "db"

    def query(self, sql, *a, **k):
        if sql is scanner._COLUMNS_SQL:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "T", "COLUMN_NAME": "Pos",
                     "DATA_TYPE": "MyPoint", "CHARACTER_MAXIMUM_LENGTH": None,
                     "NUMERIC_PRECISION": None, "NUMERIC_SCALE": None,
                     "IS_NULLABLE": "YES", "IS_CLR_TYPE": True}]
        if sql is scanner._ROWCOUNTS_SQL:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "T", "ROW_COUNT": 1, "USED_PAGES": 1}]
        return []


def test_the_scan_flags_clr_columns():
    assert "ty.is_assembly_type AS IS_CLR_TYPE" in scanner._COLUMNS_SQL
    (t,) = scanner.scan_tables(_ScanConn())
    assert t.columns[0].is_clr_type is True


# --- FULLGLOBE: no PostGIS form, so NULL plus a note (or a clear failure) ---------

def test_full_globe_count_query_quotes_names():
    assert full_globe_sql("dbo", "Odd]Name", ["Pin"]) == (
        "SELECT SUM(CASE WHEN [Pin].STGeometryType() = 'FullGlobe' THEN 1 ELSE 0 END) AS [Pin] "
        "FROM [dbo].[Odd]]Name]"
    )


def test_the_geography_cast_loads_full_globe_as_null():
    expr = cast_template("geography").format(c="[P]", a="[P]")
    assert expr.startswith("CASE WHEN [P].STGeometryType() = 'FullGlobe' THEN NULL ELSE ")


def test_the_note_names_only_columns_that_had_any():
    assert full_globe_note({"Pin": 0}) is None
    assert full_globe_note({"Pin": 2, "Area": 0}) == (
        "FULLGLOBE has no PostGIS form; stored as NULL: 2 in Pin."
    )


class _Source:
    def __init__(self, counts):
        self.counts, self.sql = counts, []

    def query(self, sql, timeout=None):
        self.sql.append(sql)
        return [self.counts]


def _spec(nullable=True):
    return TableLoadSpec(schema_name="dbo", table_name="GlobeEdge", columns=[
        ColumnInfo(name="Id", data_type="int", is_nullable=False),
        ColumnInfo(name="Pin", data_type="geography", is_nullable=nullable),
    ])


def test_sync_load_notes_full_globes_stored_as_null():
    assert _check_full_globes(_Source({"Pin": 1}), _spec()) == (
        "FULLGLOBE has no PostGIS form; stored as NULL: 1 in Pin."
    )


def test_sync_load_fails_up_front_for_a_not_null_column():
    # Otherwise COPY fails with a bare not-null violation that hides the reason.
    with pytest.raises(TableWarning) as err:
        _check_full_globes(_Source({"Pin": 1}), _spec(nullable=False))
    # The loaders prefix the table already, so the message doesn't repeat it.
    assert str(err.value).startswith("FULLGLOBE values in NOT NULL column(s) Pin ")


def test_tables_without_geography_skip_the_count():
    src = _Source({})
    spec = TableLoadSpec(schema_name="dbo", table_name="T",
                         columns=[ColumnInfo(name="Id", data_type="int")])
    assert _check_full_globes(src, spec) is None and src.sql == []


def _async_full_globes(meta, counts):
    """Exec the notebook's helper against a fake Spark reader."""
    from backend.data_migration.etl_generator import generate
    from tests.test_data_migration import _req

    code = generate(_req())[0].code
    answers = iter([meta, [counts]])

    class _Reader:
        def option(self, *a, **k):
            return self

        def load(self):
            rows = next(answers)
            return type("D", (), {"collect": lambda self: rows})()

    ns: dict = {"_reader": _Reader}
    exec(code[code.index("class TableWarning"):code.index("def _source_relation")], ns)
    return ns["_full_globes"], ns["TableWarning"]


def test_async_load_notes_and_warns_the_same_way():
    meta = [{"COLUMN_NAME": "Pin", "IS_NULLABLE": "YES"}]
    full_globes, _ = _async_full_globes(meta, {"Pin": 3})
    assert full_globes("dbo", "T") == "FULLGLOBE has no PostGIS form; stored as NULL: 3 in Pin."
    meta = [{"COLUMN_NAME": "Pin", "IS_NULLABLE": "NO"}]
    full_globes, table_warning = _async_full_globes(meta, {"Pin": 3})
    with pytest.raises(table_warning, match="NOT NULL"):
        full_globes("dbo", "T")


def test_async_tables_without_geography_get_no_note():
    full_globes, _ = _async_full_globes([], {})
    assert full_globes("dbo", "T") is None


def test_async_job_warns_instead_of_failing_on_a_table_warning():
    """The job only raises for real failures; a TableWarning table is recorded as
    a warning and left out of them."""
    from backend.data_migration.etl_generator import generate
    from tests.test_data_migration import _req

    code = generate(_req())[0].code
    loop = code[code.index("        except TableWarning as warn:"):code.index("# Restore the FKs the load dropped.")]
    assert 'warned[name] = str(warn)' in loop
    assert '"status": "warning", "warning": str(warn)' in loop
    assert loop.index("except TableWarning") < loop.index("except Exception")
    assert "failures[name]" not in loop.split("except Exception")[0]


def test_assessment_counts_full_globes_and_flags_them():
    class _Conn:
        def query(self, sql, timeout=None):
            return [{"Pin": 2}]

    t = _table(_col("int", "Id"), _col("geography", "Pin"))
    scanner._count_full_globes(_Conn(), [t])
    assert t.columns[1].full_globe_count == 2
    (f,) = check_full_globes([t])
    assert f.rule_id == "GEOGRAPHY_FULLGLOBE" and f.severity is Severity.MEDIUM
    assert "will load as NULL" in f.detail


def test_a_not_null_column_is_a_high_finding():
    t = _table(ColumnInfo(name="Pin", data_type="geography", is_nullable=False,
                          full_globe_count=1))
    (f,) = check_full_globes([t])
    assert f.severity is Severity.HIGH and "will not be loaded" in f.detail


def test_a_failed_count_does_not_break_the_assessment():
    class _Conn:
        def query(self, sql, timeout=None):
            raise RuntimeError("permission denied")

    t = _table(_col("geography", "Pin"))
    scanner._count_full_globes(_Conn(), [t])
    assert t.columns[0].full_globe_count == 0


def test_sync_run_records_a_warning_and_still_succeeds(monkeypatch):
    """A TableWarning table is a warning row, not a failure: the run ends success,
    not partial, and no resume is offered for it."""
    from backend.migration import runs
    from backend.migration.models import DataLoadRequest, LakebaseConnRequest

    def fake_load(source, target, spec, *a, **k):
        if spec.table_name == "GlobeStrict":
            raise TableWarning("FULLGLOBE values in NOT NULL column(s) Pin have no PostGIS form.")
        return 2

    monkeypatch.setattr(runs, "build_connector", lambda *a, **k: object())
    monkeypatch.setattr(runs, "LakebaseConnection", lambda **k: object())
    monkeypatch.setattr(runs, "capture_and_drop_fks", lambda *a: [])
    monkeypatch.setattr(runs, "restore_fks", lambda *a: [])
    monkeypatch.setattr(runs, "load_table", fake_load)
    monkeypatch.setattr(runs.audit, "spawn", lambda fn, *a: fn(*a))

    req = DataLoadRequest(
        host="h", database="d", username="u", password="p",
        lakebase=LakebaseConnRequest(host="lb", database="db", user="u", password="p"),
        tables=[TableLoadSpec(schema_name="dbo", table_name="GlobeEdge"),
                TableLoadSpec(schema_name="dbo", table_name="GlobeStrict")],
    )
    state = runs.get_run(runs.start_run(req))
    assert state.status == "success"
    strict = state.tables[1]
    assert strict.status == "warning" and strict.warning.startswith("FULLGLOBE values")
    assert runs.tables_left(state) == 0
