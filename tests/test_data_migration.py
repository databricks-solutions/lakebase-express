"""Snapshot code-generation for async mode — runs without a live database or workspace."""
import ast
import json
import re
import sys
from types import SimpleNamespace

from backend.assessment import scanner
from backend.assessment.models import TableInfo
from backend.data_migration.etl_generator import generate
from backend.data_migration.models import DataGenRequest, LoadMode, PostLoadStatement, TableRef


def _req(**over):
    base = dict(
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb-host", lakebase_user="lbuser",
        lakebase_password_secret_key="lb-key",
        tables=[
            TableRef(schema_name="dbo", table_name="Orders", primary_key=["OrderId"]),
            TableRef(schema_name="Sales", table_name="Invoice"),
        ],
    )
    base.update(over)
    return DataGenRequest(**base)


# A request whose post-data phase spans every object type — exercises the
# per-type notebook/task split.
def _req_with_post(**over):
    over.setdefault("post_load_sql", [
        PostLoadStatement(name="public.orders · PRIMARY KEY", kind="constraint",
                          sql='ALTER TABLE "public"."orders" ADD PRIMARY KEY ("Id");'),
        PostLoadStatement(name="public.orders · ix_orders_created", kind="index",
                          sql='CREATE INDEX IF NOT EXISTS "ix" ON "public"."orders" ("Id");'),
        PostLoadStatement(name="public.orders · fk_orders_customer", kind="foreign_key",
                          sql='ALTER TABLE "public"."orders" ADD CONSTRAINT "fk" FOREIGN KEY ...;'),
        PostLoadStatement(name="public.orders · trg_audit", kind="trigger",
                          sql='CREATE TRIGGER public.trg_audit AFTER INSERT ON public.orders '
                              'FOR EACH ROW EXECUTE FUNCTION public.trg_audit();'),
    ])
    return _req(**over)


def _parse_python_cells(code: str) -> None:
    """The generated notebook is valid Python once the notebook magics are stripped."""
    body = "\n".join(
        line
        for line in code.splitlines()
        if not line.startswith("# MAGIC")
        and line != "# Databricks notebook source"
        and line.strip() != "# COMMAND ----------"
    )
    ast.parse(body)


# --- Artifact shape --------------------------------------------------------------


def test_generates_only_snapshot_when_no_post_data():
    arts = generate(_req())
    assert [a.filename for a in arts] == ["01_snapshot_load.py"]


def test_generates_one_post_load_notebook_per_object_type():
    # One notebook per type that has statements, numbered + ordered so the job
    # chains them constraints -> indexes -> foreign keys -> triggers.
    arts = generate(_req_with_post())
    assert [a.filename for a in arts] == [
        "01_snapshot_load.py",
        "02_post_load_constraints.py",
        "03_post_load_indexes.py",
        "04_post_load_foreign_keys.py",
        "05_post_load_triggers.py",
    ]


def test_post_load_notebooks_only_for_present_types():
    # Only constraints + triggers present -> only those two post-load notebooks.
    req = _req(post_load_sql=[
        PostLoadStatement(name="pk", kind="constraint", sql="ALTER TABLE x ADD PRIMARY KEY (id);"),
        PostLoadStatement(name="trg", kind="trigger",
                          sql="CREATE TRIGGER s.t AFTER INSERT ON s.x FOR EACH ROW EXECUTE FUNCTION s.t();"),
    ])
    assert [a.filename for a in generate(req)] == [
        "01_snapshot_load.py",
        "02_post_load_constraints.py",
        "03_post_load_triggers.py",   # renumbered — indexes/FKs absent
    ]


def test_default_mode_is_snapshot():
    assert _req().mode is LoadMode.SNAPSHOT


def test_generated_notebook_is_valid_python():
    # Every generated notebook (snapshot + each per-type post-load) parses.
    for art in generate(_req_with_post()):
        _parse_python_cells(art.code)


# --- Source + target wiring ------------------------------------------------------


def test_reads_source_via_named_connector_writes_via_copy():
    code = generate(_req())[0].code
    assert 'spark.read.format("sqlserver")' in code    # source read
    # The write is Postgres COPY streamed from every partition — never a Spark
    # JDBC write (serverless blocks the generic jdbc source for DML anyway).
    assert "COPY {fq_target} ({col_list}) FROM STDIN" in code
    assert 'format("jdbc")' not in code
    assert 'format("postgresql")' not in code


def test_no_tls_options_serverless_rejects_them():
    """Serverless validates connector options against an allowlist; sslmode/encrypt
    fail with SERVERLESS_WRITE_OPTIONS_NOT_ALLOWED. The drivers negotiate TLS anyway."""
    code = generate(_req())[0].code
    assert 'option("sslmode"' not in code
    assert 'option("encrypt"' not in code


def test_snapshot_copies_direct_no_staging_double_write():
    """COPY parses text server-side against the real column types (uuid/xml/
    timestamptz), so the load goes straight into the plan-created table — no
    staging table, no INSERT .. SELECT CAST reload."""
    code = generate(_req())[0].code
    # Serverless jobs copy %pip args verbatim into a requirements file — quotes
    # around the spec become part of the requirement and fail to parse.
    assert "%pip install psycopg[binary]" in code
    assert '"psycopg[binary]"' not in code
    assert "__stg" not in code
    assert "INSERT INTO" not in code
    assert "TRUNCATE TABLE" in code                     # idempotent re-runs
    assert "to_regclass" in code                        # clear error if plan not applied


def test_snapshot_parallelizes_tables_and_source_reads():
    """30GB+ sources need the cluster busy: concurrent tables on a thread pool,
    and range-partitioned JDBC reads when the scanned primary key allows it."""
    code = generate(_req())[0].code
    assert "ThreadPoolExecutor" in code and "MAX_PARALLEL_TABLES" in code
    for opt in ("partitionColumn", "lowerBound", "upperBound", "numPartitions"):
        assert opt in code
    # The per-partition COPY runs on executors via a DataFrame API.
    assert "mapInArrow" in code


def test_snapshot_avoids_apis_unsupported_on_serverless():
    """Serverless has no sparkContext/RDD surface — the notebook must stay on
    DataFrame APIs so it runs on both serverless and classic job compute."""
    code = generate(_req())[0].code
    assert "sparkContext" not in code
    assert ".rdd" not in code
    assert "foreachPartition" not in code
    # "query" and "dbtable" on the same (mutated) reader is a JDBC error.
    assert "def _reader()" in code


def test_no_delta_landing_or_cdc_or_synced_table():
    code = generate(_req())[0].code
    for gone in ("enableChangeDataFeed", "CHANGETABLE", "SyncedDatabaseTable", "saveAsTable"):
        assert gone not in code


def test_both_passwords_read_from_secret_scope_never_embedded():
    arts = generate(_req_with_post(secret_scope="myscope"))
    code = arts[0].code
    assert code.count('dbutils.secrets.get(scope="myscope"') == 2
    assert 'key="k"' in code and 'key="lb-key"' in code
    # Post-load notebooks only talk to Lakebase — one secret each, no source creds.
    for post in (a.code for a in arts[1:]):
        assert post.count('dbutils.secrets.get(scope="myscope"') == 1
        assert 'key="lb-key"' in post and 'key="k"' not in post


def test_source_read_casts_spark_unreadable_types():
    """Spark's sqlserver reader fails whole tables containing sql_variant /
    hierarchyid / spatial columns (UNRECOGNIZED_SQL_TYPE, e.g. sql_variant id
    -156) — the read must project them through server-side casts to text,
    discovered from INFORMATION_SCHEMA at runtime."""
    code = generate(_req())[0].code
    assert "INFORMATION_SCHEMA.COLUMNS" in code
    assert '"sql_variant": "CAST({c} AS NVARCHAR(MAX)) AS {a}"' in code
    assert '"hierarchyid": "{c}.ToString() AS {a}"' in code
    assert '"geography": "{c}.STAsText() AS {a}"' in code
    assert '"geometry": "{c}.STAsText() AS {a}"' in code
    # The read resolves its relation through the projection helper, so affected
    # tables get a casting subquery while clean tables keep the raw dbtable.
    assert "dbtable = _source_relation(src_schema, src_table)" in code
    assert "FROM [{src_schema}].[{src_table}]) AS src" in code


def test_metadata_probe_has_no_order_by():
    """Spark wraps the "query" option in a derived table (SPARK_GEN_SUBQ), and SQL
    Server rejects ORDER BY inside one ("The ORDER BY clause is invalid in views,
    ... derived tables, subqueries") — the column probe must sort client-side."""
    code = generate(_req())[0].code
    assert "ORDINAL_POSITION FROM INFORMATION_SCHEMA.COLUMNS" in code
    assert "ORDER BY ORDINAL_POSITION" not in code
    assert 'sorted(meta, key=lambda r: r["ORDINAL_POSITION"])' in code


def test_one_failed_table_does_not_abort_snapshot():
    """A single bad table must not stop the others from loading; the run still
    fails at the end with a summary so the job surfaces it."""
    code = generate(_req())[0].code
    assert "except Exception as exc:" in code
    assert "failures[f" in code                        # per-table failure recorded
    assert "raise RuntimeError" in code


# --- Target name mapping mirrors the migration plan ------------------------------


def test_snapshot_rows_map_source_to_lakebase_targets():
    code = generate(_req(target_schema="public"))[0].code
    # dbo -> target schema (public); Orders lower-cased; PK rides along as the
    # partition-column candidate for the parallel read.
    assert '("dbo", "Orders", "public", "orders", "OrderId")' in code
    # Non-default schema keeps its own lower-cased name; no PK -> single stream.
    assert '("Sales", "Invoice", "sales", "invoice", None)' in code


def test_snapshot_rows_can_preserve_source_case():
    code = generate(_req(target_schema="AppCore", identifier_case="preserve"))[0].code
    assert '("dbo", "Orders", "AppCore", "Orders", "OrderId")' in code
    assert '("Sales", "Invoice", "Sales", "Invoice", None)' in code


# --- Scanner / model contract ----------------------------------------------------


def test_table_info_primary_key_round_trips():
    t = TableInfo(schema_name="dbo", table_name="Orders", row_count=0, column_count=1,
                  primary_key=["OrderId", "LineId"])
    assert t.primary_key == ["OrderId", "LineId"]
    assert TableInfo(**t.model_dump()).primary_key == ["OrderId", "LineId"]


def test_primary_key_defaults_empty():
    assert TableInfo(schema_name="dbo", table_name="X", row_count=0, column_count=0).primary_key == []


def test_scanner_has_primary_key_query():
    assert "PRIMARY KEY" in scanner._PRIMARY_KEYS_SQL
    assert "KEY_COLUMN_USAGE" in scanner._PRIMARY_KEYS_SQL


# --- Run-state reporting from the job (OAuth) ---------------------------------


def _run_store_target():
    from backend.data_migration.models import RunStoreTarget

    return RunStoreTarget(
        host="ep-x.database.azuredatabricks.net", database="databricks_postgres",
        table="lbx_runs", endpoint="projects/p/branches/production/endpoints/primary",
    )


def _spec(run_store=None, project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"):
    return DataGenRequest(
        project_id=project_id,
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb", lakebase_user="lbu", lakebase_password_secret_key="lbk",
        tables=[TableRef(schema_name="dbo", table_name="Orders")],
        post_load_sql=[PostLoadStatement(name="pk", kind="constraint",
                                         sql="ALTER TABLE x ADD PRIMARY KEY (id);")],
        run_store=run_store,
    )


def test_notebooks_report_run_state_over_oauth():
    # The job records its own state: it runs outside the app, so nothing else can.
    code = generate(_spec(_run_store_target()))[0].code
    assert "generate_database_credential" in code
    assert 'sslmode="require"' in code
    # A recent SDK is needed on the cluster for w.postgres; the app's pin is untouched.
    assert "%pip install psycopg[binary] databricks-sdk>=0.81.0" in code


def test_no_password_is_embedded_for_run_state():
    """The point of OAuth here: the reporter carries no secret at all."""
    code = generate(_spec(_run_store_target()))[0].code
    reporter = code.split("def _report_run_state")[1].split("# COMMAND")[0]
    assert "dbutils.secrets.get" not in reporter
    assert "password=token" in reporter  # the minted credential, nothing else


def test_every_task_derives_the_same_run_id():
    # The chain is load -> post-load tasks; all of them must update one row, so the
    # id comes from the job run rather than being generated per task.
    arts = generate(_spec(_run_store_target()))
    assert len(arts) >= 2
    for art in arts:
        assert "uuid.uuid5" in art.code
        assert 'dbutils.widgets.get("job_run_id")' in art.code


def test_the_notebook_derives_the_same_run_id_as_the_app():
    """The whole point of a derived id: a run this app triggered is already a row,
    and the notebook must update it rather than open a second one. Executes the
    generated function so a drifting literal or namespace cannot pass."""
    from backend.run_store import run_state_id

    code = generate(_spec(_run_store_target()))[0].code
    body = "def _run_state_id():" + (
        code.split("def _run_state_id():")[1].split("def _report_run_state")[0]
    )
    widgets = SimpleNamespace(get=lambda k: {"job_id": "100", "job_run_id": "555"}[k])
    ns: dict = {"dbutils": SimpleNamespace(widgets=widgets)}
    exec(body, ns)
    assert ns["_run_state_id"]() == run_state_id(100, 555)


def test_the_notebook_merges_its_payload_into_the_row():
    """On the run-now path the app wrote the row first; a replacing update would
    drop the job url, notebook path and table count it recorded."""
    code = generate(_spec(_run_store_target()))[0].code
    assert ".data || (EXCLUDED.data - 'tasks')" in code
    assert "data = EXCLUDED.data" not in code
    # "tasks" is merged a level deeper, or each task would erase the others.
    assert "jsonb_build_object('tasks'" in code


def test_every_task_reports_its_own_start_and_finish():
    """Only the loader used to report, so a run showed nothing but the snapshot."""
    arts = generate(_spec(_run_store_target()))
    assert len(arts) > 1
    for art in arts:
        assert '_report_run_state("running")' in art.code, art.filename
        assert '_report_run_state("success")' in art.code, art.filename
        assert '_report_run_state("failed"' in art.code or \
               '_report_run_state(\n        "failed"' in art.code, art.filename


def test_the_recorded_task_keys_are_the_jobs_own():
    """Recorded state is only useful next to the Jobs UI if the keys line up."""
    from backend.data_migration.etl_generator import run_task_keys, task_key

    arts = generate(_spec(_run_store_target()))
    keys = run_task_keys(_spec(_run_store_target()))
    assert keys == [task_key(a.filename.removesuffix(".py")) for a in arts]
    for art, key in zip(arts, keys):
        assert f'RUN_STATE_PHASE = "{key}"' in art.code
        # Each task carries the whole chain, so it knows if it is the last.
        assert f"RUN_STATE_TASKS = {keys!r}" in art.code


def test_without_a_run_store_the_reporter_is_an_inert_stub():
    # No fallback credential exists, so unconfigured means no reporting — the
    # calls stay valid and do nothing.
    code = generate(_spec(None))[0].code
    assert "generate_database_credential" not in code
    assert "databricks-sdk" not in code
    assert "def _report_run_state(status, error=None):\n    pass" in code


def test_the_project_link_reaches_the_notebook():
    code = generate(_spec(_run_store_target()))[0].code
    assert 'RUN_STORE_PROJECT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"' in code


# --- Executing the generated reporter -------------------------------------------
#
# The status/timing logic lives inside the generated notebook, where no import can
# reach it. These tests run that code against fakes and inspect what it would write,
# so a mistake in the template is caught here rather than in a real job.


class _Cur:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append((sql, params))


class _Conn:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._sink)

    def commit(self):
        pass


def _reporter(monkeypatch, code, phase_calls=None):
    """Exec a notebook's run-state cell and return (namespace, writes)."""
    start = code.index("RUN_STORE_HOST = ")
    block = code[start:code.index("# COMMAND", start)]

    writes: list[tuple] = []
    psycopg = SimpleNamespace(connect=lambda **kw: _Conn(writes))
    credential = SimpleNamespace(
        generate_database_credential=lambda endpoint: SimpleNamespace(token="tok")
    )
    sdk = SimpleNamespace(WorkspaceClient=lambda: SimpleNamespace(
        postgres=credential,
        current_user=SimpleNamespace(me=lambda: SimpleNamespace(user_name="me@x.com")),
    ))
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)

    widgets = SimpleNamespace(
        text=lambda *a, **k: None,
        get=lambda k: {"job_id": "100", "job_run_id": "555"}[k],
    )
    ns: dict = {"dbutils": SimpleNamespace(widgets=widgets)}
    exec(block, ns)
    for status, error in phase_calls or []:
        ns["_report_run_state"](status, error)
    return ns, [json.loads(p[4]) for _sql, p in writes]


def test_a_task_records_its_own_start_and_finish(monkeypatch):
    load = generate(_spec(_run_store_target()))[0]
    _ns, rows = _reporter(monkeypatch, load.code, [("running", None), ("success", None)])

    started, finished = (r["tasks"]["load"] for r in rows)
    assert started["status"] == "running" and started["finished_at"] is None
    assert finished["status"] == "success" and finished["finished_at"] is not None
    # The finish keeps the start it was given, so a duration is computable.
    assert finished["started_at"] == started["started_at"]
    assert finished["finished_at"] >= finished["started_at"]


def test_the_run_is_not_finished_until_its_last_task_is(monkeypatch):
    """The bug this fixes: the loader's success used to mark the whole run success
    while four post-load tasks were still to run."""
    arts = generate(_spec(_run_store_target()))
    assert len(arts) > 1

    _ns, load_rows = _reporter(monkeypatch, arts[0].code, [("running", None), ("success", None)])
    assert [r["status"] for r in load_rows] == ["running", "running"]
    assert load_rows[0]["started_at"] is not None       # the run's own start
    assert "finished_at" not in load_rows[1]

    _ns, last_rows = _reporter(monkeypatch, arts[-1].code, [("running", None), ("success", None)])
    assert [r["status"] for r in last_rows] == ["running", "success"]
    assert last_rows[-1]["finished_at"] is not None
    # Only the first task stamps the run's start.
    assert "started_at" not in last_rows[0]


def test_a_failure_anywhere_fails_the_run(monkeypatch):
    mid = generate(_spec(_run_store_target()))[1]
    _ns, rows = _reporter(monkeypatch, mid.code, [("running", None), ("failed", "boom")])

    assert rows[-1]["status"] == "failed"
    assert rows[-1]["finished_at"] is not None
    task = rows[-1]["tasks"][rows[-1]["phase"]]
    assert task["status"] == "failed" and task["error"] == "boom"


def test_each_task_writes_only_its_own_entry(monkeypatch):
    """Whole-object writes would erase the sibling tasks; the SQL merges instead."""
    arts = generate(_spec(_run_store_target()))
    keys = set()
    for art in arts:
        _ns, rows = _reporter(monkeypatch, art.code, [("running", None)])
        assert list(rows[0]["tasks"]) == [rows[0]["phase"]]
        keys.add(rows[0]["phase"])
    from backend.data_migration.etl_generator import run_task_keys

    assert keys == set(run_task_keys(_spec(_run_store_target())))


def test_the_emitted_upsert_is_valid_postgres(monkeypatch):
    """There is no local Postgres to run it against, so parse what the notebook
    actually builds — a merge expression this dense is easy to get wrong."""
    import pglast

    load = generate(_spec(_run_store_target()))[0]
    start = load.code.index("RUN_STORE_HOST = ")
    block = load.code[start:load.code.index("# COMMAND", start)]

    writes: list[tuple] = []
    monkeypatch.setitem(sys.modules, "psycopg",
                        SimpleNamespace(connect=lambda **kw: _Conn(writes)))
    monkeypatch.setitem(sys.modules, "databricks.sdk", SimpleNamespace(
        WorkspaceClient=lambda: SimpleNamespace(
            postgres=SimpleNamespace(
                generate_database_credential=lambda endpoint: SimpleNamespace(token="t")),
            current_user=SimpleNamespace(me=lambda: SimpleNamespace(user_name="me@x.com")))))
    ns: dict = {"dbutils": SimpleNamespace(widgets=SimpleNamespace(
        text=lambda *a, **k: None, get=lambda k: {"job_id": "1", "job_run_id": "2"}[k]))}
    exec(block, ns)
    ns["_report_run_state"]("running")

    sql = writes[0][0]
    counter = iter(range(1, 20))
    pglast.parse_sql(re.sub(r"%s", lambda _m: f"${next(counter)}", sql))
