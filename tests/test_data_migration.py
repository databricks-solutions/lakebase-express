"""Snapshot code-generation for async mode — runs without a live database or workspace."""
import ast

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
