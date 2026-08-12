"""Post-data constraint/index/FK generation — runs without a live database."""
import ast

from backend.assessment import scanner
from backend.assessment.models import (
    CheckConstraintInfo,
    ColumnDefaultInfo,
    ColumnInfo,
    ForeignKeyInfo,
    IndexColumnInfo,
    IndexInfo,
    ProgrammableObject,
    TableInfo,
)
from backend.data_migration.etl_generator import generate
from backend.data_migration.models import DataGenRequest, PostLoadStatement, TableRef
from backend.migration.executor import _ordered
from backend.migration.models import KIND_ORDER, POST_DATA_KINDS, ObjectKind
from backend.migration.planner import build_plan
from backend.schema_migration.ddl_generator import (
    check_constraint_ddl,
    column_default_ddl,
    foreign_key_ddl,
    generate_ddl,
    identity_ddl,
    index_ddl,
    post_data_ddl,
    primary_key_ddl,
)
from backend.schema_migration.expr_mapper import map_expression
from backend.schema_migration.naming import IdentifierCase
from backend.schema_migration.windows_timezones import WINDOWS_TO_IANA


def _rich_table() -> TableInfo:
    return TableInfo(
        schema_name="dbo", table_name="Orders", row_count=10, column_count=3,
        columns=[
            ColumnInfo(name="Id", data_type="int", is_nullable=False),
            ColumnInfo(name="CustomerId", data_type="int"),
            ColumnInfo(name="CreatedAt", data_type="datetime2"),
        ],
        primary_key=["Id"],
        identity_column="Id",
        foreign_keys=[
            ForeignKeyInfo(
                name="FK_Orders_Customers", columns=["CustomerId"],
                ref_schema="Sales", ref_table="Customers", ref_columns=["Id"],
                on_delete="CASCADE", on_update="NO_ACTION",
            )
        ],
        indexes=[
            IndexInfo(
                name="IX_Orders_CreatedAt",
                columns=[IndexColumnInfo(name="CreatedAt", descending=True)],
                include_columns=["CustomerId"],
                is_unique=False,
                filter_definition="([CustomerId] IS NOT NULL)",
            )
        ],
        column_defaults=[ColumnDefaultInfo(column="CreatedAt", definition="(getdate())")],
        check_constraints=[CheckConstraintInfo(name="CK_Orders_Id", definition="([Id]>(0))")],
    )


# --- Expression mapping ------------------------------------------------------------


def test_expression_brackets_and_nstrings():
    assert map_expression("([Status]=N'open')") == '("Status"=\'open\')'


def test_expression_function_map():
    assert map_expression("(getdate())") == "(now())"
    assert map_expression("(newid())") == "(gen_random_uuid())"
    assert map_expression("(isnull([A],(0)))") == '(coalesce("A",(0)))'
    assert map_expression("(len([Code])>(3))") == '(length("Code")>(3))'
    assert map_expression("(getutcdate())") == "((now() AT TIME ZONE 'utc'))"


def test_expression_unknown_passthrough():
    # Unknown constructs stay verbatim: they fail visibly at apply time and can
    # be edited in the plan, instead of silently changing meaning. (CONVERT with a
    # translatable target type is rewritten — see the cast tests below.)
    assert map_expression("(dateadd(day,(30),getdate()))") == "(dateadd(day,(30),now()))"
    assert map_expression("(format([D],'yyyy-MM'))") == '(format("D",\'yyyy-MM\'))'


# --- Post-data DDL builders --------------------------------------------------------


def test_primary_key_ddl_is_guarded_and_composite():
    t = _rich_table()
    ddl = primary_key_ddl(t, "public")
    assert '\'"public"."orders"\'::regclass' in ddl and "contype = 'p'" in ddl
    assert 'ADD CONSTRAINT "pk_orders" PRIMARY KEY ("Id")' in ddl

    t.primary_key = ["OrderId", "LineId"]
    assert 'PRIMARY KEY ("OrderId", "LineId")' in primary_key_ddl(t, "public")


def test_foreign_key_ddl_maps_schemas_and_actions():
    fk = _rich_table().foreign_keys[0]
    ddl = foreign_key_ddl(fk, "Orders", "public", "public")
    assert 'ALTER TABLE "public"."orders" ADD CONSTRAINT "fk_orders_customers"' in ddl
    assert 'FOREIGN KEY ("CustomerId")' in ddl
    # Referenced schema mapped like the rest of the plan (Sales -> sales).
    assert 'REFERENCES "sales"."customers" ("Id")' in ddl
    assert "ON DELETE CASCADE" in ddl
    assert "ON UPDATE" not in ddl                      # NO_ACTION is the default
    assert "conname = 'fk_orders_customers'" in ddl    # idempotency guard


def test_post_data_identifiers_preserve_case_when_requested():
    table = _rich_table()
    pk = primary_key_ddl(table, "AppCore", IdentifierCase.PRESERVE)
    assert 'ALTER TABLE "AppCore"."Orders" ADD CONSTRAINT "pk_Orders"' in pk

    fk = foreign_key_ddl(
        table.foreign_keys[0], "Orders", "AppCore", "public", IdentifierCase.PRESERVE
    )
    assert 'ADD CONSTRAINT "FK_Orders_Customers"' in fk
    assert 'REFERENCES "Sales"."Customers"' in fk

    idx = index_ddl(table.indexes[0], "Orders", "AppCore", IdentifierCase.PRESERVE)
    assert idx.startswith('CREATE INDEX IF NOT EXISTS "Orders_IX_Orders_CreatedAt"')
    assert 'ON "AppCore"."Orders"' in idx


def test_index_ddl_unique_include_desc_and_filter():
    idx = _rich_table().indexes[0]
    ddl = index_ddl(idx, "Orders", "public")
    # Table-prefixed name: source index names are per-table, PG's are per-schema.
    assert ddl.startswith('CREATE INDEX IF NOT EXISTS "orders_ix_orders_createdat"')
    assert 'ON "public"."orders" ("CreatedAt" DESC)' in ddl
    assert 'INCLUDE ("CustomerId")' in ddl
    assert 'WHERE ("CustomerId" IS NOT NULL)' in ddl

    idx.is_unique = True
    assert index_ddl(idx, "Orders", "public").startswith("CREATE UNIQUE INDEX IF NOT EXISTS")


def test_column_default_and_check_ddl_translate_expressions():
    t = _rich_table()
    d = column_default_ddl(t.column_defaults[0], "Orders", "public")
    assert d == 'ALTER TABLE "public"."orders" ALTER COLUMN "CreatedAt" SET DEFAULT (now());'
    c = check_constraint_ddl(t.check_constraints[0], "Orders", "public")
    assert 'ADD CONSTRAINT "ck_orders_id" CHECK (("Id">(0)))' in c
    assert "conname = 'ck_orders_id'" in c


def test_bit_column_default_becomes_boolean():
    # A bit column maps to Postgres boolean; its ((1))/((0)) default must become
    # true/false — an integer default is rejected ("column is of type boolean but
    # default expression is of type integer").
    bit = ColumnInfo(name="is_active", data_type="bit")
    on = column_default_ddl(ColumnDefaultInfo(column="is_active", definition="((1))"),
                            "Instrument", "ref", column=bit)
    off = column_default_ddl(ColumnDefaultInfo(column="is_active", definition="((0))"),
                             "Instrument", "ref", column=bit)
    assert on.endswith('SET DEFAULT true;')
    assert off.endswith('SET DEFAULT false;')


def test_bit_comparison_in_filtered_index_becomes_boolean():
    # A filtered index on a bit column ("WHERE ([Active]=(1))") failed to apply:
    # bit maps to Postgres boolean, and "boolean = integer" is an error there
    # rather than a silent coercion. The predicate needs the column's type, which
    # a DEFAULT gets but an index filter historically did not.
    cols = [ColumnInfo(name="Email", data_type="nvarchar", max_length=120),
            ColumnInfo(name="Active", data_type="bit")]
    idx = IndexInfo(name="IX_Active", columns=[IndexColumnInfo(name="Email")],
                    filter_definition="([Active]=(1))")
    ddl = index_ddl(idx, "CollationSearch", "public", IdentifierCase.LOWERCASE, cols)
    assert 'WHERE ("Active" = (true))' in ddl
    assert "=(1)" not in ddl


def test_bit_comparison_keeps_parentheses_balanced():
    # The literal's own parens must be kept and the predicate's outer paren left
    # alone; a greedy match would swallow it and emit unbalanced SQL.
    cols = [ColumnInfo(name="Active", data_type="bit")]
    for src, want in [
        ("([Active]=(1))", '("Active" = (true))'),
        ("([Active]=1)", '("Active" = true)'),
        ("([Active]<>(0))", '("Active" <> (false))'),
        ("((([Active]=(1))))", '((("Active" = (true))))'),
    ]:
        got = map_expression(src, columns=cols)
        assert got == want, f"{src} -> {got}"
        assert got.count("(") == got.count(")")


def test_bit_rewrite_only_touches_bit_columns():
    cols = [ColumnInfo(name="Active", data_type="bit"),
            ColumnInfo(name="Qty", data_type="int"),
            ColumnInfo(name="Code", data_type="nvarchar", max_length=5)]
    # An int compared to 1 is already valid Postgres; a '1' string literal is not
    # a boolean; IS NULL works on boolean unchanged. None may be rewritten.
    assert map_expression("([Qty]=(1))", columns=cols) == '("Qty"=(1))'
    assert map_expression("([Code]='1')", columns=cols) == '("Code"=\'1\')'
    assert map_expression("([Active] IS NULL)", columns=cols) == '("Active" IS NULL)'
    # Not a 0/1 literal: left verbatim to fail visibly rather than be guessed at.
    assert map_expression("([Active]=(2))", columns=cols) == '("Active"=(2))'
    # No column context: unchanged, so callers that can't supply it still work.
    assert map_expression("([Active]=(1))") == '("Active"=(1))'


def test_bit_comparison_in_check_constraint_becomes_boolean():
    cols = [ColumnInfo(name="Active", data_type="bit")]
    ddl = check_constraint_ddl(
        CheckConstraintInfo(name="CK_Active", definition="([Active]=(1))"),
        "Thing", "public", IdentifierCase.LOWERCASE, cols,
    )
    assert 'CHECK (("Active" = (true)))' in ddl


def test_convert_default_becomes_postgres_cast():
    # (CONVERT([datetime2](7),getdate())) is a very common SQL Server default. The
    # bracketed type must not survive as a quoted identifier: "datetime2"(7) reads
    # as a call to a function that doesn't exist ("function datetime2(integer) does
    # not exist"). datetime2(7) is 100 ns; Postgres timestamps stop at 6, so the
    # out-of-range precision is dropped rather than rejected as timestamp(7).
    col = ColumnInfo(name="UpdatedAt", data_type="datetime2")
    sql = column_default_ddl(
        ColumnDefaultInfo(column="UpdatedAt", definition="(CONVERT([datetime2](7),getdate()))"),
        "Assets", "public", column=col,
    )
    assert sql.endswith("SET DEFAULT (CAST(now() AS timestamp));")
    assert "datetime2" not in sql

    # An in-range precision is preserved.
    assert "CAST(now() AS timestamp(3))" in column_default_ddl(
        ColumnDefaultInfo(column="UpdatedAt", definition="(CONVERT([datetime2](3),getdate()))"),
        "Assets", "public", column=col,
    )


def test_cast_and_convert_forms_translate_in_checks_and_indexes():
    # CAST(val AS type) and CONVERT(type, val) reach Postgres as the same CAST,
    # including when nested; the same mapper backs checks and filtered indexes.
    assert map_expression("(CAST(getdate() AS [datetime2](7)))") == "(CAST(now() AS timestamp))"
    assert map_expression("(CONVERT([int],[Qty]))") == '(CAST("Qty" AS integer))'
    assert map_expression("(CONVERT([varchar](max),[Notes]))") == '(CAST("Notes" AS text))'
    assert map_expression("(CONVERT([decimal](18,2),[Amt]))") == '(CAST("Amt" AS numeric(18,2)))'
    assert (
        map_expression("(CAST(CONVERT([datetime2](7),getdate()) AS [date]))")
        == "(CAST(CAST(now() AS timestamp) AS date))"
    )
    assert (
        map_expression("(CONVERT([date],[CreatedAt]) IS NOT NULL)")
        == '(CAST("CreatedAt" AS date) IS NOT NULL)'
    )


def test_untranslatable_conversions_are_left_verbatim_to_fail_visibly():
    # Per this module's policy, anything that can't be translated faithfully is
    # left as written so it fails at apply time instead of silently changing
    # meaning. A 3-arg CONVERT's style code has no Postgres equivalent; spatial
    # types have no mapping; bare char/decimal have different implicit widths in
    # the two engines (T-SQL char(30)/decimal(18,0) vs PG char(1)/unbounded).
    assert map_expression("(CONVERT([varchar](10),getdate(),(112)))") == (
        '(CONVERT("varchar"(10),now(),(112)))'
    )
    assert map_expression("(CONVERT([geography],[Shape]))") == '(CONVERT("geography","Shape"))'
    assert map_expression("(CONVERT([char],[Code]))") == '(CONVERT("char","Code"))'
    assert map_expression("(CONVERT([decimal],[Amt]))") == '(CONVERT("decimal","Amt"))'

    # The inner expression is still translated even when the call itself isn't.
    assert map_expression("(CONVERT([geography],getdate()))") == '(CONVERT("geography",now()))'


def test_conversion_rewrite_respects_string_literals_and_bad_input():
    # A conversion spelled inside a string literal is a value, not code.
    assert map_expression("('CONVERT([int],x)')") == '(\'CONVERT("int",x)\')'
    # Parens and escaped quotes inside literals must not confuse paren matching.
    assert map_expression("(CONVERT([varchar](10),')'))") == "(CAST(')' AS varchar(10)))"
    assert map_expression("(CONVERT([varchar](10),'it''s ('))") == "(CAST('it''s (' AS varchar(10)))"
    # Malformed/degenerate input passes through instead of raising.
    for expr in ("", "(CONVERT())", "(CONVERT([int]))", "(CAST([a] AS))"):
        map_expression(expr)


def test_windows_time_zone_name_becomes_iana():
    # SQL Server AT TIME ZONE names zones in the Windows registry form; Postgres
    # only knows IANA names and rejects the Windows name at execution ("time zone
    # ... not recognized"). Only the quoted name is rewritten — the clause is
    # identical in both engines. Lookup is case-insensitive (SQL Server's is).
    assert (
        map_expression("(getdate() AT TIME ZONE 'E. South America Standard Time')")
        == "(now() AT TIME ZONE 'America/Sao_Paulo')"
    )
    assert (
        map_expression("(getdate() AT TIME ZONE 'pacific standard time')")
        == "(now() AT TIME ZONE 'America/Los_Angeles')"
    )
    # Already-IANA and unknown names are left as written (fail-visibly for the latter).
    assert "America/Sao_Paulo" in map_expression("(getdate() AT TIME ZONE 'America/Sao_Paulo')")
    assert map_expression("(d AT TIME ZONE 'Nowhere Standard Time')") == (
        "(d AT TIME ZONE 'Nowhere Standard Time')"
    )


def test_windows_timezone_table_is_populated_and_iana_valued():
    # Guards a bad regeneration of the vendored CLDR map (empty / non-IANA values).
    assert len(WINDOWS_TO_IANA) > 100
    assert WINDOWS_TO_IANA["E. South America Standard Time"] == "America/Sao_Paulo"
    # Every value is an IANA-style zone (Region/City or the Etc/ family), never a
    # Windows display name, and no key is itself already IANA.
    assert all("/" in v for v in WINDOWS_TO_IANA.values())
    assert not any("/" in k for k in WINDOWS_TO_IANA)


def test_customer_convert_with_windows_time_zone_default():
    # The exact shape a customer hit: CONVERT to datetime2(3) around a getdate()
    # shifted with a Windows time-zone name. Both defects must clear — the
    # datetime2 pseudo-function AND the untranslated Windows zone — or the retry
    # just trades one execution error for another.
    col = ColumnInfo(name="UpdatedAt", data_type="datetime2")
    sql = column_default_ddl(
        ColumnDefaultInfo(
            column="UpdatedAt",
            definition="(CONVERT([datetime2](3),(getdate() AT TIME ZONE 'E. South America Standard Time')))",
        ),
        "Assets", "public", column=col,
    )
    assert sql.endswith(
        "SET DEFAULT (CAST((now() AT TIME ZONE 'America/Sao_Paulo') AS timestamp(3)));"
    )
    assert "datetime2" not in sql
    assert "Standard Time" not in sql


def test_next_value_for_default_creates_sequence_and_uses_nextval():
    # T-SQL NEXT VALUE FOR [schema].[seq] -> nextval('schema.seq'), and the
    # referenced sequence is created first so the default isn't dangling.
    col = ColumnInfo(name="ticket_no", data_type="bigint")
    sql = column_default_ddl(
        ColumnDefaultInfo(column="ticket_no", definition="(NEXT VALUE FOR [trading].[seq_order_ticket])"),
        "Order", "trading", column=col, target_schema="public",
    )
    assert 'CREATE SEQUENCE IF NOT EXISTS "trading"."seq_order_ticket";' in sql
    assert "SET DEFAULT nextval('trading.seq_order_ticket');" in sql
    # Unqualified sequence falls back to the target schema.
    sql2 = column_default_ddl(
        ColumnDefaultInfo(column="n", definition="(NEXT VALUE FOR [seq_x])"),
        "T", "public", column=ColumnInfo(name="n", data_type="int"), target_schema="public",
    )
    assert "nextval('public.seq_x')" in sql2

    preserved = column_default_ddl(
        ColumnDefaultInfo(column="ticket_no", definition="(NEXT VALUE FOR [Trading].[SeqOrder])"),
        "Order", "Trading", column=col, target_schema="public",
        identifier_case=IdentifierCase.PRESERVE,
    )
    assert 'CREATE SEQUENCE IF NOT EXISTS "Trading"."SeqOrder";' in preserved
    assert '''nextval('"Trading"."SeqOrder"')''' in preserved


def test_identity_ddl_integer_becomes_identity_with_sequence_sync():
    ddl = identity_ddl(_rich_table(), "public")
    assert 'ADD GENERATED BY DEFAULT AS IDENTITY' in ddl
    assert "attidentity = ''" in ddl                       # idempotency guard
    # The sequence syncs to MAX+1 — the reason identity is a post-data step.
    assert "pg_get_serial_sequence" in ddl and 'MAX("Id")' in ddl


def test_identity_ddl_non_integer_falls_back_to_sequence_default():
    t = TableInfo(
        schema_name="dbo", table_name="Invoice", row_count=0, column_count=1,
        columns=[ColumnInfo(name="No", data_type="numeric", precision=18, scale=0)],
        identity_column="No",
    )
    ddl = identity_ddl(t, "public")
    assert 'CREATE SEQUENCE IF NOT EXISTS "public"."invoice_no_seq"' in ddl
    assert 'SET DEFAULT nextval' in ddl
    assert "GENERATED" not in ddl                          # identity is int-only in PG


# --- Planner phase emission --------------------------------------------------------


def test_plan_emits_post_data_items_with_kinds():
    items = build_plan([_rich_table()], [], "public", translate=False, endpoint=None)
    by_id = {i.id: i for i in items}
    assert by_id["pk:dbo.Orders"].kind is ObjectKind.CONSTRAINT
    assert by_id["identity:dbo.Orders.Id"].kind is ObjectKind.CONSTRAINT
    assert by_id["default:dbo.Orders.CreatedAt"].kind is ObjectKind.CONSTRAINT
    assert by_id["check:dbo.Orders.CK_Orders_Id"].kind is ObjectKind.CONSTRAINT
    assert by_id["index:dbo.Orders.IX_Orders_CreatedAt"].kind is ObjectKind.INDEX
    assert by_id["fk:dbo.Orders.FK_Orders_Customers"].kind is ObjectKind.FOREIGN_KEY


def test_apply_order_constraints_then_indexes_then_fks_then_triggers():
    objs = [ProgrammableObject(schema_name="dbo", object_name="trg", object_type="TRIGGER",
                               line_count=1, definition="x")]
    items = _ordered(build_plan([_rich_table()], objs, "public", translate=False, endpoint=None))
    order = [i.kind for i in items]
    assert order.index(ObjectKind.TABLE) < order.index(ObjectKind.CONSTRAINT)
    assert order.index(ObjectKind.CONSTRAINT) < order.index(ObjectKind.INDEX)
    assert order.index(ObjectKind.INDEX) < order.index(ObjectKind.FOREIGN_KEY)
    # Triggers moved to the post-data phase: they must not fire during the COPY.
    assert order.index(ObjectKind.FOREIGN_KEY) < order.index(ObjectKind.TRIGGER)


def test_post_data_kinds_cover_the_post_phase_only():
    assert POST_DATA_KINDS == {
        ObjectKind.CONSTRAINT, ObjectKind.INDEX, ObjectKind.FOREIGN_KEY, ObjectKind.TRIGGER
    }
    assert set(KIND_ORDER) == set(ObjectKind)              # executor can sort every kind


def test_plain_table_produces_no_post_data_items():
    t = TableInfo(schema_name="dbo", table_name="Log", row_count=0, column_count=1,
                  columns=[ColumnInfo(name="Msg", data_type="nvarchar", max_length=100)])
    items = build_plan([t], [], "public", translate=False, endpoint=None)
    assert [i for i in items if i.kind in POST_DATA_KINDS] == []


def test_generate_ddl_appends_post_data_section():
    script, count = generate_ddl([_rich_table()], "public")
    assert "Post-data phase — run AFTER the data load" in script
    assert script.index("CREATE TABLE") < script.index("PRIMARY KEY")
    # schema + table + 6 post-data statements (default, identity, pk, check, index, fk)
    assert count == 8
    assert len(post_data_ddl([_rich_table()], "public")) == 6


# --- Async notebook embedding ------------------------------------------------------


def _parse_python_cells(code: str) -> None:
    body = "\n".join(
        line
        for line in code.splitlines()
        if not line.startswith("# MAGIC")
        and line != "# Databricks notebook source"
        and line.strip() != "# COMMAND ----------"
    )
    ast.parse(body)


def _req(**over):
    base = dict(
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb-host", lakebase_user="lbuser",
        lakebase_password_secret_key="lb-key",
        tables=[TableRef(schema_name="dbo", table_name="Orders", primary_key=["OrderId"])],
    )
    base.update(over)
    return DataGenRequest(**base)


def _post_by_type(arts):
    """Map post-load notebook filename stem -> code."""
    return {a.filename.removesuffix(".py"): a.code for a in arts[1:]}


def test_post_load_notebook_embeds_statements_per_type():
    tricky_sql = 'DO $$\nBEGIN\n    RAISE NOTICE \'it''s "quoted"\';\nEND $$;'
    req = _req(post_load_sql=[
        PostLoadStatement(name="public.orders · PRIMARY KEY", kind="constraint", sql=tricky_sql),
        PostLoadStatement(name="public.orders · idx", kind="index",
                          sql='CREATE INDEX IF NOT EXISTS "i" ON "public"."orders" ("OrderId");'),
        PostLoadStatement(name="empty is dropped", kind="constraint", sql="   "),
    ])
    arts = generate(req)
    snapshot = arts[0].code
    assert "POST_LOAD" not in snapshot
    by_type = _post_by_type(arts)
    # One notebook per present type; the constraint statement lands in the
    # constraints notebook, the index in the indexes notebook.
    assert set(by_type) == {"02_post_load_constraints", "03_post_load_indexes"}
    con = by_type["02_post_load_constraints"]
    assert "POST_LOAD = [" in con
    assert "'public.orders · PRIMARY KEY'" in con
    assert "empty is dropped" not in con                   # blank SQL never rides along
    assert "public.orders · idx" not in con                # index went to its own notebook
    assert "public.orders · idx" in by_type["03_post_load_indexes"]
    # Per-item progress + a final failure summary, re-runnable alone.
    assert "[{i}/{total}]" in con
    assert "raise RuntimeError" in con
    _parse_python_cells(con)                               # repr keeps it valid Python


def test_post_load_notebook_sanitizes_stale_trigger_sql():
    # A plan built before the trigger fix embeds schema-qualified, plain CREATE
    # TRIGGER SQL. The notebook must still emit valid, idempotent DDL — the
    # sanitizer runs at emit time, not only at translation time.
    req = _req(post_load_sql=[
        PostLoadStatement(name="trading.trg_order_audit", kind="trigger",
                          sql="CREATE TRIGGER trading.trg_order_audit AFTER INSERT ON trading.\"order\" "
                              "FOR EACH ROW EXECUTE FUNCTION trading.trg_order_audit();"),
    ])
    trg_nb = _post_by_type(generate(req))["02_post_load_triggers"]
    assert "CREATE OR REPLACE TRIGGER trg_order_audit AFTER" in trg_nb
    assert "TRIGGER trading.trg_order_audit AFTER" not in trg_nb   # schema stripped
    assert "EXECUTE FUNCTION trading.trg_order_audit()" in trg_nb  # function qualifier kept


def test_snapshot_notebook_guards_fks_and_triggers():
    arts = generate(_req())
    code = arts[0].code
    assert "def drop_target_fks()" in code
    assert "def restore_target_fks(dropped)" in code
    assert "pg_get_constraintdef" in code
    # Triggers are disabled around each table's COPY.
    assert "DISABLE TRIGGER USER" in code and "ENABLE TRIGGER USER" in code
    _parse_python_cells(code)


# --- Scanner / model contract ------------------------------------------------------


def test_new_table_info_fields_default_empty_for_old_reports():
    t = TableInfo(schema_name="dbo", table_name="X", row_count=0, column_count=0)
    assert t.identity_column is None
    assert t.foreign_keys == [] and t.indexes == []
    assert t.column_defaults == [] and t.check_constraints == []
    assert TableInfo(**t.model_dump()).foreign_keys == []


def test_scanner_has_constraint_queries():
    assert "sys.foreign_keys" in scanner._FOREIGN_KEYS_SQL
    assert "sys.indexes" in scanner._INDEXES_SQL
    assert "is_primary_key = 0" in scanner._INDEXES_SQL    # PK recreated as constraint
    assert "sys.default_constraints" in scanner._DEFAULTS_SQL
    assert "sys.check_constraints" in scanner._CHECKS_SQL
    assert "sys.identity_columns" in scanner._IDENTITY_SQL
