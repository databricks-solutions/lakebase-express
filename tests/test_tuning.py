"""Load-tuning recommendations (backend/data_migration/tuning.py, capacity.py) and
how the snapshot notebook applies them — no live database needed."""
import threading
import time

from backend.api import data_routes
from backend.assessment import scanner
from backend.data_migration import capacity
from backend.data_migration.etl_generator import generate
from backend.data_migration.models import (
    CapacityLimits,
    DataGenRequest,
    TableRef,
    TuningOverrides,
)
from backend.data_migration.tuning import DEFAULT_STREAMS, MAX_STREAMS, recommend
from backend.migration import planner
from backend.schema_migration import ai_translator

_GB = 1024 ** 3


def _t(name, rows=None, size=0, pk=("Id",), pk_type="bigint") -> TableRef:
    return TableRef(schema_name="dbo", table_name=name, primary_key=list(pk),
                    row_count=rows, data_bytes=size, primary_key_type=pk_type)


def _limits(**over) -> CapacityLimits:
    base = dict(target_max_connections=225, target_reserved_connections=3,
                target_connections_in_use=6, source_service_objective="GP_Gen5_4",
                source_cpus=4, source_max_workers=410)
    base.update(over)
    return CapacityLimits(**base)


def _by_name(plan):
    return {tp.table_name: tp for tp in plan.tables}


# --- Stream budget -------------------------------------------------------------------


def test_the_tighter_side_sets_the_stream_budget():
    # Target: (225 - 3 - 6) * 75% = 162. Source: 4 vCores * 4 = 16, half of 410 workers = 205.
    plan = recommend([_t("A", 10, 1)], _limits(), TuningOverrides())
    assert plan.max_streams == 16
    assert any("GP_Gen5_4" in r for r in plan.reasons)


def test_a_small_target_limits_the_streams():
    plan = recommend([_t("A", 10, 1)], _limits(target_max_connections=20, source_cpus=32),
                     TuningOverrides())
    assert plan.max_streams == 8   # (20 - 3 - 6) * 75%


def test_streams_are_capped():
    plan = recommend([_t("A", 10, 1)], _limits(target_max_connections=5000, source_cpus=80,
                                               source_max_workers=None), TuningOverrides())
    assert plan.max_streams == MAX_STREAMS


def test_unknown_limits_keep_the_old_default():
    plan = recommend([_t("A", 10, 1)], None, TuningOverrides())
    assert plan.max_streams == DEFAULT_STREAMS
    assert "unknown" in plan.reasons[0]


def test_one_side_unknown_is_said_so():
    plan = recommend([_t("A", 10, 1)], CapacityLimits(target_max_connections=60), TuningOverrides())
    assert plan.max_streams == 45   # 60 * 75%
    assert any("Source limits unknown" in r for r in plan.reasons)


def test_probe_notes_ride_along_as_reasons():
    plan = recommend([_t("A", 10, 1)], CapacityLimits(notes=["Source not connected"]),
                     TuningOverrides())
    assert "Source not connected" in plan.reasons


# --- Per-table chunks, streams and fetch size ------------------------------------------


def test_chunks_follow_table_size():
    plan = recommend([_t("Big", 50_000_000, 4 * _GB), _t("Small", 100, 16_384)],
                     _limits(source_cpus=16, source_max_workers=None), TuningOverrides())
    tables = _by_name(plan)
    assert (tables["Big"].chunks, tables["Big"].streams) == (16, 16)   # 4 GB / 256 MB
    assert (tables["Small"].chunks, tables["Small"].streams) == (1, 1)


def test_a_huge_table_on_a_small_source_gets_many_small_chunks():
    """The failed run: 127 GB over 8 streams was 4 tasks of ~32 GB each. Chunk size
    no longer depends on the streams — only how many load at once does."""
    plan = recommend([_t("Huge", 370_000_000, 127 * _GB), _t("Small", 100, 16_384)],
                     _limits(source_cpus=2, source_max_workers=200), TuningOverrides())
    huge = _by_name(plan)["Huge"]
    assert (plan.max_streams, huge.streams) == (8, 4)
    assert huge.chunks == 508                            # ~256 MB each
    assert "508 chunks of ~256.0 MB, 4 at a time" in huge.reason


def test_a_lone_table_may_use_every_stream():
    plan = recommend([_t("Big", 50_000_000, 12 * _GB)], _limits(), TuningOverrides())
    assert (plan.tables[0].chunks, plan.tables[0].streams) == (48, 16)


def test_tables_that_cannot_be_cut_into_ranges_get_one_chunk():
    plan = recommend([_t("NoPk", 9_000_000, 2 * _GB, pk=()),
                      _t("Guid", 9_000_000, 2 * _GB, pk_type="uniqueidentifier")],
                     _limits(), TuningOverrides())
    tables = _by_name(plan)
    assert (tables["NoPk"].chunks, tables["NoPk"].streams) == (1, 1)
    assert "no primary key" in tables["NoPk"].reason
    assert tables["Guid"].chunks == 1 and "uniqueidentifier" in tables["Guid"].reason


def test_row_count_sizes_when_the_scan_predates_table_size():
    plan = recommend([_t("A", 10_000_000), _t("B", 1)], _limits(), TuningOverrides())
    assert _by_name(plan)["A"].chunks == 5   # 10M / 2M rows


def test_unknown_size_keeps_the_old_partition_count():
    plan = recommend([_t("A"), _t("B")], None, TuningOverrides())
    assert (_by_name(plan)["A"].chunks, _by_name(plan)["A"].streams) == (8, 8)


def test_empty_tables_read_in_one_chunk():
    plan = recommend([_t("A", 0)], _limits(), TuningOverrides())
    assert (plan.tables[0].chunks, plan.tables[0].streams) == (1, 1)


def test_fetch_size_follows_row_width():
    plan = recommend([_t("Narrow", 1_000_000, 100_000_000),      # ~100 B/row
                      _t("Wide", 1_000_000, 50 * _GB),           # ~53 KB/row
                      _t("Unknown", 1_000_000)],
                     _limits(), TuningOverrides())
    tables = _by_name(plan)
    assert tables["Narrow"].fetch_size == 50_000   # capped
    assert tables["Wide"].fetch_size == 500        # floor
    assert tables["Unknown"].fetch_size == 10_000


def test_largest_tables_load_first():
    plan = recommend([_t("S", 1, 1), _t("L", 1, 3 * _GB), _t("M", 1, _GB)], _limits(),
                     TuningOverrides())
    assert [tp.table_name for tp in plan.tables] == ["L", "M", "S"]


def test_parallel_tables_default_to_what_the_streams_allow():
    tables = [_t(f"T{i}", 1, 1) for i in range(40)]
    assert recommend(tables, _limits(), TuningOverrides()).max_parallel_tables == 16
    assert recommend(tables[:3], _limits(), TuningOverrides()).max_parallel_tables == 3


# --- Overrides -----------------------------------------------------------------------


def test_overrides_win_and_are_listed():
    o = TuningOverrides(max_streams=10, table_streams=4, fetch_size=5000)
    plan = recommend([_t("A", 50_000_000, 4 * _GB), _t("B", 1, 1)], _limits(), o)
    tables = _by_name(plan)
    assert (plan.max_streams, plan.max_parallel_tables) == (10, 2)
    assert (tables["A"].chunks, tables["A"].streams) == (16, 4)
    assert tables["B"].streams == 1                 # never more streams than chunks
    assert {tp.fetch_size for tp in plan.tables} == {5000}
    assert plan.warnings == []
    assert {"Streams set by the project: 10.", "Streams per table set by the project: 4.",
            "Rows per fetch set by the project: 5,000."} <= set(plan.reasons)


def test_overrides_are_clamped_into_range():
    o = TuningOverrides(max_streams=1000, table_streams=0, fetch_size=2, translate_workers=99)
    assert (o.max_streams, o.table_streams, o.fetch_size, o.translate_workers) == (256, 1, 500, 64)


def test_a_saved_partitions_override_is_dropped():
    # Partitions per table became streams per table; the old value no longer applies.
    assert TuningOverrides.model_validate({"num_partitions": 1}).table_streams is None


def test_a_saved_tables_at_once_override_is_ignored():
    # The field is gone: tables at once always follows the streams.
    o = TuningOverrides.model_validate({"max_parallel_tables": 2})
    assert recommend([_t(f"T{i}", 1, 1) for i in range(5)], _limits(), o).max_parallel_tables == 5


def test_streams_past_the_target_limit_are_warned_about():
    plan = recommend([_t("A", 1, 1)], _limits(), TuningOverrides(max_streams=250))
    assert plan.max_streams == 250
    assert any("too many connections" in w for w in plan.warnings)
    assert any("slow its own workload" in w for w in plan.warnings)


def test_table_streams_past_the_per_table_cap_are_clamped_and_warned_about():
    plan = recommend([_t("A", 1, 10 * _GB), _t("B", 1, 10 * _GB)], _limits(),
                     TuningOverrides(table_streams=32))
    assert {tp.streams for tp in plan.tables} == {8}
    assert any("capped at 8" in w for w in plan.warnings)


# --- Live probes -----------------------------------------------------------------------


class _Fake:
    def __init__(self, answers):
        self.answers = answers

    def query(self, sql, *a, **k):
        for key, rows in self.answers.items():
            if key in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        raise AssertionError(sql)


def test_azure_sql_limits_come_from_the_service_objective():
    src = _Fake({"dm_user_db_resource_governance": [
        {"slo_name": "GP_Gen5_4", "cpu_limit": 4, "primary_group_max_workers": 410}]})
    got = capacity.probe_source(src)
    assert (got["source_service_objective"], got["source_cpus"], got["source_max_workers"]) == (
        "GP_Gen5_4", 4.0, 410)


def test_the_portal_service_objective_name_wins():
    src = _Fake({"DATABASEPROPERTYEX": [{"slo": "HS_PRMS_2"}],
                 "dm_user_db_resource_governance": [
                     {"slo_name": "SQLDB_HS_PRMS_ON_8AM_2_INTERNAL", "cpu_limit": 2,
                      "primary_group_max_workers": 200}]})
    assert capacity.probe_source(src)["source_service_objective"] == "HS_PRMS_2"


def test_sql_server_falls_back_to_the_host():
    src = _Fake({"dm_user_db_resource_governance": RuntimeError("Invalid object name"),
                 "dm_os_sys_info": [{"cpu_count": 8, "max_workers_count": 576}]})
    got = capacity.probe_source(src)
    assert (got["source_cpus"], got["source_max_workers"]) == (8.0, 576)


def test_unreadable_source_limits_are_a_note_not_an_error():
    src = _Fake({"dm_user_db_resource_governance": RuntimeError("denied"),
                 "dm_os_sys_info": RuntimeError("denied")})
    got = capacity.probe_source(src)
    assert "VIEW DATABASE STATE" in got["notes"][0]
    assert "source_cpus" not in got


def test_target_limits():
    tgt = _Fake({"max_connections": [{"max_connections": 225, "reserved": 3, "in_use": 6}]})
    limits = capacity.probe(None, tgt)
    assert (limits.target_max_connections, limits.target_reserved_connections,
            limits.target_connections_in_use) == (225, 3, 6)
    assert any("Source not connected" in n for n in limits.notes)


def test_the_route_reuses_limits_it_is_given(monkeypatch):
    monkeypatch.setattr(capacity, "probe", lambda *a: (_ for _ in ()).throw(AssertionError))
    res = data_routes.tuning(data_routes.TuningRequest(
        tables=[_t("A", 1, 1)], capacity=_limits(), overrides=TuningOverrides(max_streams=5)))
    assert res.plan.max_streams == 5 and res.capacity.source_cpus == 4


def test_the_route_probes_with_no_connections_without_failing():
    res = data_routes.tuning(data_routes.TuningRequest(tables=[_t("A", 1, 1)]))
    assert res.plan.max_streams == DEFAULT_STREAMS
    assert len(res.capacity.notes) == 2


# --- Scanner -----------------------------------------------------------------------------


class _ScanConn:
    database = "db"

    def query(self, sql, *a, **k):
        if sql is scanner._COLUMNS_SQL:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "Orders", "COLUMN_NAME": "Id",
                     "DATA_TYPE": "int", "CHARACTER_MAXIMUM_LENGTH": None,
                     "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 0, "IS_NULLABLE": "NO"}]
        if sql is scanner._ROWCOUNTS_SQL:
            return [{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "Orders", "ROW_COUNT": 10,
                     "USED_PAGES": 3}]
        return []


def test_the_scan_sizes_each_table():
    assert "used_page_count" in scanner._ROWCOUNTS_SQL
    (orders,) = scanner.scan_tables(_ScanConn())
    assert orders.data_bytes == 3 * 8192


# --- The generated notebook ----------------------------------------------------------------


def _req(tables, **over) -> DataGenRequest:
    return DataGenRequest(host="h", database="db", username="u", password_secret_key="k",
                          lakebase_host="lb", lakebase_user="lbu",
                          lakebase_password_secret_key="lbk", tables=tables, **over)


def test_the_notebook_carries_the_recommendation():
    code = generate(_req([_t("Small", 100, 16_384), _t("Big", 50_000_000, 4 * _GB)],
                         capacity=_limits()))[0].code
    assert "MAX_STREAMS = 16" in code and "MAX_PARALLEL_TABLES = 2" in code
    assert "#   Source GP_Gen5_4" in code
    # Largest first, each with its own chunks, streams and fetch size, and why.
    big = code.index('("dbo", "Big", "public", "big", "Id", 16, 8, ')
    assert big < code.index('("dbo", "Small", "public", "small", "Id", 1, 1, ')
    assert "4.0 GB → 16 chunks of ~256.0 MB, 8 at a time" in code


def test_warnings_reach_the_notebook():
    code = generate(_req([_t("A", 1, 1)], capacity=_limits(),
                         tuning=TuningOverrides(max_streams=250)))[0].code
    assert "#   WARNING: 250 streams" in code


def test_a_reason_cannot_break_out_of_its_comment():
    code = generate(_req([_t("A", 1, 1)],
                         capacity=CapacityLimits(notes=["line one\nimport os"])))[0].code
    assert "\nimport os" not in code


def _budget_ns(code: str, max_streams: int, copy):
    ns: dict = {"threading": threading, "MAX_STREAMS": max_streams, "snapshot_table": copy}
    exec(code[code.index("class _StreamBudget"):code.index("def _merge_fks")], ns)
    return ns


def test_streams_are_shared_largest_first_and_never_exceeded():
    """Run the notebook's scheduler for real: concurrency stays within MAX_STREAMS,
    tables start in TABLES order, and an oversized ask is capped instead of hanging."""
    from concurrent.futures import ThreadPoolExecutor

    code = generate(_req([_t("A", 1, 1)]))[0].code
    lock, live, peak, started = threading.Lock(), [0], [0], []

    def copy(s, t, _ds, _dt, _p, _chunks, streams, _f):
        with lock:
            started.append(t)
            live[0] += streams
            peak[0] = max(peak[0], live[0])
        time.sleep(0.02)
        with lock:
            live[0] -= streams
        return streams, None, None

    ns = _budget_ns(code, 8, copy)
    budget = ns["_StreamBudget"](8)
    specs = [("dbo", name, "public", name, "Id", 100, n, 10000)
             for name, n in [("huge", 50), ("big", 4), ("mid", 4), ("s1", 1), ("s2", 1)]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = [f.result(timeout=5) for f in
               [pool.submit(ns["_load"], budget, turn, spec) for turn, spec in enumerate(specs)]]
    assert got[0][0] == 8                      # 50 capped to MAX_STREAMS
    assert peak[0] <= 8
    assert started[:3] == ["huge", "big", "mid"]


# --- Chunked reads, run for real -----------------------------------------------------------


def _chunk_ns(code: str, **fakes) -> dict:
    """The notebook's chunk helpers and snapshot_table, exec'd against fakes."""
    import datetime
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ns: dict = {"datetime": datetime, "ThreadPoolExecutor": ThreadPoolExecutor,
                "as_completed": as_completed, "PG_KWARGS": {}}
    exec(code[code.index("def _chunk_edges"):code.index("class _StreamBudget")], ns)
    ns.update(fakes)
    return ns


_CODE = generate(_req([_t("A", 1, 1)]))[0].code


def test_chunk_edges_split_ints_evenly():
    edges = _chunk_ns(_CODE)["_chunk_edges"](1, 1001, 4)
    assert edges == [251, 501, 751]


def test_chunk_edges_never_repeat_on_a_narrow_range():
    assert _chunk_ns(_CODE)["_chunk_edges"](1, 3, 10) == [2]


def test_chunk_edges_on_dates_and_datetimes():
    import datetime as dt
    edges = _chunk_ns(_CODE)["_chunk_edges"]
    assert edges(dt.date(2020, 1, 1), dt.date(2020, 1, 5), 2) == [dt.date(2020, 1, 3)]
    got = edges(dt.datetime(2020, 1, 1, 0, 0, 0, 5), dt.datetime(2020, 1, 1, 0, 0, 3), 3)
    assert got == [dt.datetime(2020, 1, 1, 0, 0, 1), dt.datetime(2020, 1, 1, 0, 0, 2)]


def test_chunk_ranges_are_open_ended_and_leave_no_gaps():
    ns = _chunk_ns(_CODE)
    where = ns["_chunk_where"]
    assert where("Id", None, 10) == " WHERE ([Id] < 10 OR [Id] IS NULL)"
    assert where("Id", 10, 20) == " WHERE [Id] >= 10 AND [Id] < 20"
    assert where("Id", 20, None) == " WHERE [Id] >= 20"
    import datetime as dt
    assert where("D", dt.datetime(2020, 1, 2, 3, 4, 5), None) == " WHERE [D] >= '2020-01-02T03:04:05'"


def test_a_chunk_narrows_the_projected_relation():
    narrow = _chunk_ns(_CODE)["_narrow"]
    assert narrow("(SELECT [Id] FROM [dbo].[T]) AS src", " WHERE [Id] >= 5") == (
        "(SELECT [Id] FROM [dbo].[T] WHERE [Id] >= 5) AS src")
    assert narrow("[dbo].[T]", " WHERE [Id] >= 5") == "(SELECT * FROM [dbo].[T] WHERE [Id] >= 5) AS src"
    assert narrow("[dbo].[T]", "") == "[dbo].[T]"


class _PgFake:
    """psycopg stand-in: the target exists and has no triggers."""
    executed: list

    def __init__(self):
        self.executed = []

    def connect(self, **kw):
        fake = self

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return _Cur()

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                fake.executed.append(sql)
                self.last = sql

            def fetchone(self):
                return (False,) if "pg_trigger" in self.last else ("public.t",)

        return _Conn()


def _snapshot_fakes(copy_chunk, bounds=(1, 1001)):
    reader = type("R", (), {"option": lambda self, *a: self,
                            "load": lambda self: type("D", (), {"columns": ["Id"]})()})
    return dict(_source_relation=lambda s, t: "(SELECT [Id] FROM [dbo].[T]) AS src",
                _full_globes=lambda s, t: None,
                _bounds=lambda rel, col: bounds, _reader=lambda *a: reader(),
                psycopg=_PgFake(), _copy_chunk=copy_chunk, _utcnow=lambda: "now")


def test_a_table_loads_chunk_by_chunk_within_its_streams():
    lock, live, peak, seen = threading.Lock(), [0], [0], []

    def copy_chunk(relation, col, edges, fetch, target, cols):
        with lock:
            seen.append(edges)
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.01)
        with lock:
            live[0] -= 1
        return 10

    ns = _chunk_ns(_CODE, **_snapshot_fakes(copy_chunk))
    rows, *_ = ns["snapshot_table"]("dbo", "T", "public", "t", "Id", 20, 3, 5000)
    assert rows == 200 and len(seen) == 20
    assert peak[0] <= 3
    # Every key lands in exactly one chunk.
    assert sorted(seen, key=lambda e: (e[0] is not None, e[0] or 0))[0][0] is None
    assert sum(1 for lo, hi in seen if hi is None) == 1
    assert "TRUNCATE TABLE \"public\".\"t\"" in ns["psycopg"].executed


def test_one_failed_chunk_fails_the_table_and_stops_the_rest():
    calls = []

    def copy_chunk(relation, col, edges, fetch, target, cols):
        calls.append(edges)
        if len(calls) == 1:
            raise RuntimeError("OOM")
        time.sleep(0.01)
        return 1

    ns = _chunk_ns(_CODE, **_snapshot_fakes(copy_chunk))
    try:
        ns["snapshot_table"]("dbo", "T", "public", "t", "Id", 50, 2, 5000)
        raise AssertionError("expected the chunk failure to surface")
    except RuntimeError as exc:
        assert "OOM" in str(exc)
    assert len(calls) < 50


def test_a_table_without_bounds_is_one_chunk():
    seen = []
    ns = _chunk_ns(_CODE, **_snapshot_fakes(lambda *a: seen.append(a[2]) or 7, bounds=None))
    rows, *_ = ns["snapshot_table"]("dbo", "T", "public", "t", "Id", 20, 4, 5000)
    assert (rows, seen) == (7, [(None, None)])


# --- Translation workers ---------------------------------------------------------------------


def test_translate_workers_reach_the_translator(monkeypatch):
    seen = {}

    def fake_translate_all(objects, endpoint=None, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(planner, "translate_all", fake_translate_all)
    obj = ai_translator.ProgrammableObject(schema_name="dbo", object_name="p", object_type="PROCEDURE",
                                           line_count=1, definition="CREATE PROCEDURE dbo.p AS SELECT 1")
    planner.build_plan([], [obj], "public", True, None, translate_workers=3)
    assert seen["workers"] == 3


def test_translate_all_honours_the_worker_count(monkeypatch):
    peak, live, lock = [0], [0], threading.Lock()

    def fake(obj, *a, **k):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.02)
        with lock:
            live[0] -= 1
        return obj

    monkeypatch.setattr(ai_translator, "translate_object", fake)
    ai_translator.translate_all(list(range(12)), workers=2)
    assert peak[0] <= 2
