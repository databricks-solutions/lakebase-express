"""Post-migration query parity: comparison logic, read-only guard, AI generation."""
import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from backend.assessment.models import ColumnInfo, TableInfo
from backend.query_parity import generator, runner
from backend.query_parity.comparator import compare, side_result
from backend.query_parity.models import ParityStatus, SideResult, SyntheticQuery


def _query(source="SELECT 1", target="SELECT 1"):
    return SyntheticQuery(id="q1", title="Test", category="read",
                          source_sql=source, target_sql=target)


def _rows(*tuples, cols=("a", "b")):
    return [dict(zip(cols, t)) for t in tuples]


# --- comparator: verdicts -----------------------------------------------------------


def test_identical_results_match():
    src = side_result(_rows((1, "x"), (2, "y")), duration_ms=10)
    tgt = side_result(_rows((1, "x"), (2, "y")), duration_ms=5)
    cmp = compare(_query(), src, tgt, _rows((1, "x"), (2, "y")), _rows((1, "x"), (2, "y")))
    assert cmp.status is ParityStatus.MATCH
    assert cmp.count_match and cmp.format_match
    # target 5ms / source 10ms → 0.5 (target twice as fast)
    assert cmp.speedup_ratio == 0.5


def test_row_count_mismatch():
    src = side_result(_rows((1, "x"), (2, "y")), duration_ms=10)
    tgt = side_result(_rows((1, "x")), duration_ms=10)
    cmp = compare(_query(), src, tgt, _rows((1, "x"), (2, "y")), _rows((1, "x")))
    assert cmp.status is ParityStatus.MISMATCH
    assert not cmp.count_match
    assert "row counts differ" in cmp.detail


def test_value_drift_is_mismatch_even_with_equal_counts():
    src = side_result(_rows((1, "x")), duration_ms=10)
    tgt = side_result(_rows((1, "z")), duration_ms=10)
    cmp = compare(_query(), src, tgt, _rows((1, "x")), _rows((1, "z")))
    assert cmp.status is ParityStatus.MISMATCH
    assert cmp.count_match and not cmp.format_match
    # The detail names how many rows and which columns differ.
    assert "1 of 1 compared row" in cmp.detail and "columns: b" in cmp.detail
    # And the structured diff pinpoints the differing cell for the UI.
    assert cmp.mismatch_columns == ["b"]
    assert len(cmp.row_diffs) == 1
    d = cmp.row_diffs[0]
    assert d.row_index == 0 and d.kind == "value" and d.diff_columns == ["b"]
    assert d.source_cells == ["1", "x"] and d.target_cells == ["1", "z"]


def test_side_result_keeps_row_preview():
    src = side_result(_rows((1, "x"), (2, "y")), duration_ms=10)
    assert src.preview_rows == [["1", "x"], ["2", "y"]]
    assert not src.truncated


def test_row_count_mismatch_records_extra_rows_in_diff():
    src = side_result(_rows((1, "x"), (2, "y")), duration_ms=10)
    tgt = side_result(_rows((1, "x")), duration_ms=10)
    cmp = compare(_query(), src, tgt, _rows((1, "x"), (2, "y")), _rows((1, "x")))
    assert cmp.status is ParityStatus.MISMATCH and not cmp.count_match
    # The row present only in the source is captured for the preview grid.
    extra = [d for d in cmp.row_diffs if d.kind == "source_only"]
    assert len(extra) == 1 and extra[0].source_cells == ["2", "y"]


def test_matching_results_have_no_row_diffs():
    src = side_result(_rows((1, "x")), duration_ms=10)
    tgt = side_result(_rows((1, "x")), duration_ms=10)
    cmp = compare(_query(), src, tgt, _rows((1, "x")), _rows((1, "x")))
    assert cmp.status is ParityStatus.MATCH
    assert cmp.row_diffs == [] and cmp.mismatch_columns == []


def test_column_names_differ_only_by_case_still_match():
    # Postgres folds unquoted identifiers to lower case, so a case-only column
    # name difference must not read as a format mismatch.
    src = side_result(_rows((1, "x"), cols=("Id", "Name")), duration_ms=10)
    tgt = side_result(_rows((1, "x"), cols=("id", "name")), duration_ms=10)
    cmp = compare(_query(), src, tgt,
                  _rows((1, "x"), cols=("Id", "Name")), _rows((1, "x"), cols=("id", "name")))
    assert cmp.status is ParityStatus.MATCH and cmp.format_match


def test_numeric_and_datetime_values_normalize_across_dialects():
    # Decimal("10.00") vs int 10, and datetime precision, must compare equal.
    src = side_result([{"n": 10, "t": datetime(2020, 1, 1, 0, 0, 0)}], duration_ms=10)
    tgt = side_result([{"n": Decimal("10.00"), "t": datetime(2020, 1, 1)}], duration_ms=10)
    cmp = compare(_query(), src, tgt,
                  [{"n": 10, "t": datetime(2020, 1, 1, 0, 0, 0)}],
                  [{"n": Decimal("10.00"), "t": datetime(2020, 1, 1)}])
    assert cmp.status is ParityStatus.MATCH


def test_error_on_one_side_is_error_status():
    src = side_result(_rows((1, "x")), duration_ms=10)
    tgt = SideResult(error="relation does not exist")
    cmp = compare(_query(), src, tgt, _rows((1, "x")), [])
    assert cmp.status is ParityStatus.ERROR
    assert "target" in cmp.detail and cmp.speedup_ratio is None


# --- read-only guard ----------------------------------------------------------------


def test_read_only_accepts_select_and_cte():
    assert runner.is_read_only("SELECT * FROM t")
    assert runner.is_read_only("  select 1  ")
    assert runner.is_read_only("WITH x AS (SELECT 1) SELECT * FROM x")
    assert runner.is_read_only("SELECT 1;")  # single trailing terminator is fine
    assert runner.is_read_only("-- a comment\nSELECT 1")


def test_read_only_rejects_writes_and_multistatement():
    assert not runner.is_read_only("INSERT INTO t VALUES (1)")
    assert not runner.is_read_only("UPDATE t SET a = 1")
    assert not runner.is_read_only("DELETE FROM t")
    assert not runner.is_read_only("DROP TABLE t")
    assert not runner.is_read_only("SELECT * INTO t2 FROM t")  # T-SQL materialization
    assert not runner.is_read_only("SELECT 1; DROP TABLE t")   # second statement
    assert not runner.is_read_only("")
    assert not runner.is_read_only("EXEC sp_who")


# --- generator ----------------------------------------------------------------------


def _table():
    return TableInfo(
        schema_name="dbo", table_name="Orders", row_count=100, column_count=2,
        columns=[ColumnInfo(name="Id", data_type="int", is_nullable=False),
                 ColumnInfo(name="Total", data_type="money")],
        primary_key=["Id"],
    )


def _resp(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason,
    )])


def _gen_payload():
    return {"queries": [{
        "title": "Order count",
        "intent": "count rows",
        "category": "aggregation",
        "source_sql": "SELECT COUNT(*) FROM [dbo].[Orders]",
        "target_sql": 'SELECT COUNT(*) FROM "public"."orders"',
    }]}


def test_generate_requests_structured_output_and_parses(monkeypatch):
    sent = {}

    def fake(endpoint, messages, **params):
        sent.update(params)
        # The schema context should carry both source and mapped target names.
        user = messages[-1].content
        assert "[dbo].[Orders]" in user and '"public"."orders"' in user
        return _resp(json.dumps(_gen_payload()))

    monkeypatch.setattr(generator, "query_chat", fake)
    out = generator.generate_queries([_table()], count=1, endpoint="my-endpoint")
    assert out.success and len(out.queries) == 1
    q = out.queries[0]
    assert q.title == "Order count" and q.category == "aggregation"
    assert q.source_sql.startswith("SELECT COUNT(*)")
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["max_tokens"] >= 8000


def test_generate_drops_queries_missing_a_dialect(monkeypatch):
    payload = {"queries": [
        {"title": "ok", "intent": "", "category": "read",
         "source_sql": "SELECT 1", "target_sql": "SELECT 1"},
        {"title": "half", "intent": "", "category": "read",
         "source_sql": "SELECT 2", "target_sql": ""},  # dropped
    ]}
    monkeypatch.setattr(generator, "query_chat", lambda e, messages, **p: _resp(json.dumps(payload)))
    out = generator.generate_queries([_table()], count=2, endpoint="e")
    assert out.success and len(out.queries) == 1 and out.queries[0].title == "ok"


def test_generate_without_tables_fails_soft():
    out = generator.generate_queries([], count=3, endpoint="e")
    assert not out.success and "No tables" in out.error


def test_generate_reports_token_limit_truncation(monkeypatch):
    monkeypatch.setattr(generator, "query_chat",
                        lambda e, messages, **p: _resp('{"queries": [', "length"))
    out = generator.generate_queries([_table()], count=3, endpoint="e")
    assert not out.success and "token limit" in out.error


def test_generate_is_fail_soft(monkeypatch):
    def boom(endpoint, messages, **params):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(generator, "query_chat", boom)
    out = generator.generate_queries([_table()], count=3, endpoint="e")
    assert not out.success and "endpoint down" in out.error


# --- run registry -------------------------------------------------------------------


def test_run_registry_unknown_id_returns_none():
    assert runner.get_run("does-not-exist") is None
