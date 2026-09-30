"""Scratch-collection analysis: `#temp` / `##temp` / `@t TABLE` -> Postgres shape.

The product rule under test is a negative one — nothing in this path may recommend
a Postgres TEMP TABLE — so the last test guards it across every surface that
carries the advice.
"""
import re

from backend.assessment import temp_objects
from backend.assessment.compatibility import check_temp_objects, run_all_rules
from backend.assessment.models import ColumnInfo, ProgrammableObject, Severity, TableInfo
from backend.assessment.temp_objects import (
    LARGE_COLLECTION_ROWS,
    Strategy,
    analyze,
    find_temp_objects,
    temp_table_regression,
)
from backend.context_bundle import rules as bundle_rules
from backend.schema_migration import ai_translator


def _proc(body: str) -> ProgrammableObject:
    return ProgrammableObject(
        schema_name="dbo", object_name="usp_x", object_type="PROCEDURE",
        line_count=body.count("\n") + 1, definition=body,
    )


def _table(name: str, rows: int) -> TableInfo:
    return TableInfo(
        schema_name="dbo", table_name=name, row_count=rows, column_count=1,
        columns=[ColumnInfo(name="id", data_type="int")],
    )


def _strategies(body: str, tables=()) -> dict[str, Strategy]:
    row_counts = {t.fqn.lower(): t.row_count for t in tables}
    return {u.obj.name: u.strategy for u in analyze(body, row_counts)}


# --- Detection --------------------------------------------------------------------


def test_finds_each_kind_of_collection():
    objs = find_temp_objects(
        "CREATE TABLE #local (id int); DECLARE @var TABLE (id int); "
        "SELECT * FROM ##shared;"
    )
    assert {(o.name, o.kind) for o in objs} == {
        ("#local", temp_objects.LOCAL_TEMP),
        ("@var", temp_objects.TABLE_VARIABLE),
        ("##shared", temp_objects.GLOBAL_TEMP),
    }


def test_global_temp_is_not_also_read_as_a_local_one():
    names = {o.name for o in find_temp_objects("SELECT * FROM ##shared")}
    assert names == {"##shared"}


def test_comments_and_scalar_variables_are_not_collections():
    body = """
    -- staging used to live in #old_stage
    /* and in #older_stage */
    DECLARE @rowcount int = 0;
    SELECT 1;
    """
    assert find_temp_objects(body) == []


def test_counts_writes_reads_index_and_mutation():
    body = """
    CREATE TABLE #stage (id int);
    INSERT INTO #stage SELECT id FROM dbo.Orders;
    CREATE INDEX ix ON #stage(id);
    UPDATE #stage SET id = id + 1;
    SELECT * FROM dbo.Customers c JOIN #stage s ON c.id = s.id;
    """
    obj = find_temp_objects(body)[0]
    assert obj.writes == 1 and obj.reads >= 1
    assert obj.indexed and obj.mutated


def test_declared_primary_key_counts_as_indexed():
    obj = find_temp_objects("DECLARE @ids TABLE (id int PRIMARY KEY); SELECT * FROM @ids")[0]
    assert obj.indexed


def test_feeding_table_row_count_comes_from_the_scan():
    body = "INSERT INTO #stage SELECT id FROM dbo.Orders WHERE x = 1; SELECT * FROM #stage"
    obj = find_temp_objects(body, {"dbo.orders": 4_200_000})[0]
    assert obj.row_source == "dbo.orders" and obj.max_rows == 4_200_000


def test_a_table_only_joined_by_a_reader_is_not_taken_as_the_source():
    body = "INSERT INTO #ids SELECT id FROM dbo.Small; SELECT * FROM dbo.Huge h JOIN #ids i ON h.id = i.id"
    obj = find_temp_objects(body, {"dbo.small": 10, "dbo.huge": 9_000_000})[0]
    assert obj.max_rows == 10


# --- Classification ---------------------------------------------------------------


def test_single_producer_single_consumer_becomes_a_cte():
    body = "INSERT INTO #stage SELECT id FROM dbo.Orders; SELECT * FROM #stage"
    assert _strategies(body) == {"#stage": Strategy.CTE}


def test_read_by_several_statements_becomes_a_plpgsql_variable():
    body = """
    DECLARE @ids TABLE (id int);
    INSERT INTO @ids SELECT id FROM dbo.Orders;
    SELECT count(*) FROM @ids;
    SELECT * FROM dbo.Customers c JOIN @ids i ON c.id = i.id;
    """
    assert _strategies(body) == {"@ids": Strategy.PLPGSQL}


def test_a_table_variable_is_only_recognised_through_its_declare():
    """Otherwise every scalar `@parameter` in a procedure would look like one."""
    assert find_temp_objects("SELECT * FROM @ids WHERE @flag = 1") == []


def test_mutated_after_fill_becomes_a_plpgsql_variable():
    body = "INSERT INTO #t SELECT id FROM dbo.Orders; DELETE FROM #t WHERE id < 0; SELECT * FROM #t"
    assert _strategies(body) == {"#t": Strategy.PLPGSQL}


def test_indexed_collection_needs_a_working_table():
    body = """
    INSERT INTO #stage SELECT id FROM dbo.Orders;
    CREATE INDEX ix ON #stage(id);
    SELECT * FROM #stage;
    """
    assert _strategies(body) == {"#stage": Strategy.WORKING_TABLE}


def test_large_collection_needs_a_working_table_and_small_one_does_not():
    body = "INSERT INTO #stage SELECT id FROM dbo.Orders; SELECT * FROM #stage"
    big = [_table("Orders", LARGE_COLLECTION_ROWS)]
    small = [_table("Orders", LARGE_COLLECTION_ROWS - 1)]
    assert _strategies(body, big) == {"#stage": Strategy.WORKING_TABLE}
    assert _strategies(body, small) == {"#stage": Strategy.CTE}


def test_size_only_escalates_when_row_counts_are_supplied():
    body = "INSERT INTO #stage SELECT id FROM dbo.Orders; SELECT * FROM #stage"
    assert _strategies(body) == {"#stage": Strategy.CTE}


def test_global_temp_needs_a_working_table():
    body = "INSERT INTO ##shared SELECT id FROM dbo.Orders; SELECT * FROM ##shared"
    assert _strategies(body) == {"##shared": Strategy.WORKING_TABLE}


def test_collection_built_in_dynamic_sql_is_left_for_review():
    body = "EXEC('INSERT INTO #stage SELECT id FROM dbo.Orders'); SELECT * FROM #stage"
    assert _strategies(body) == {"#stage": Strategy.REVIEW}


# --- Findings ---------------------------------------------------------------------


def test_findings_group_per_object_and_escalate_on_size():
    body = """
    INSERT INTO #stage SELECT id FROM dbo.Orders;
    CREATE INDEX ix ON #stage(id);
    SELECT * FROM #stage;
    """
    findings = check_temp_objects([_table("Orders", 5_000_000)], [_proc(body)])
    assert len(findings) == 1
    assert findings[0].rule_id == "TEMP_TABLE"
    assert findings[0].severity is Severity.HIGH
    assert "#stage" in findings[0].detail


def test_several_collections_sharing_a_rewrite_make_one_finding():
    body = """
    INSERT INTO #a SELECT id FROM dbo.Orders; SELECT * FROM #a;
    INSERT INTO #b SELECT id FROM dbo.Orders; SELECT * FROM #b;
    """
    findings = check_temp_objects([], [_proc(body)])
    assert len(findings) == 1
    assert "#a" in findings[0].detail and "#b" in findings[0].detail


def test_regex_rule_no_longer_double_reports_the_same_collection():
    body = "INSERT INTO #stage SELECT id FROM dbo.Orders; SELECT * FROM #stage"
    temp_findings = [f for f in run_all_rules([], [_proc(body)]) if f.rule_id == "TEMP_TABLE"]
    assert len(temp_findings) == 1


def test_a_body_with_no_collections_reports_nothing():
    assert check_temp_objects([], [_proc("SELECT 1")]) == []


# --- Guardrail on the translation -------------------------------------------------


def test_translated_temp_table_is_flagged():
    body = "INSERT INTO #stage SELECT id FROM dbo.Orders; SELECT * FROM #stage"
    assert temp_table_regression(body, "CREATE TEMP TABLE stage AS SELECT 1;")
    assert temp_table_regression(body, "CREATE TEMPORARY TABLE stage (id int);")
    assert temp_table_regression(body, "WITH stage AS (SELECT 1) SELECT * FROM stage") == ""


def test_guardrail_says_something_different_when_a_relation_was_warranted():
    indexed = """
    INSERT INTO #stage SELECT id FROM dbo.Orders;
    CREATE INDEX ix ON #stage(id);
    SELECT * FROM #stage;
    """
    message = temp_table_regression(indexed, "CREATE TEMP TABLE stage (id int);")
    assert "UNLOGGED" in message


def test_translator_prompt_carries_the_per_collection_decision():
    body = """
    INSERT INTO #stage SELECT id FROM dbo.Orders;
    CREATE INDEX ix ON #stage(id);
    SELECT * FROM #stage;
    """
    prompt = ai_translator._build_user_prompt(_proc(body))
    assert "#stage" in prompt and "working table" in prompt.lower()


def test_translator_prompt_is_unchanged_for_a_body_with_no_collections():
    prompt = ai_translator._build_user_prompt(_proc("SELECT 1"))
    assert "scratch collections" not in prompt


# --- The negative rule, across every surface --------------------------------------


# Words that turn a mention of a temp table into a prohibition. Checked in the run-up
# to the phrase, so "not a TEMP TABLE" passes and "use a TEMP TABLE" does not.
_NEGATIONS = ("not", "never", "no ", "none", "cannot", "instead of", "rather than", "without")
_LOOKBEHIND = 90


def _mentions_are_all_negated(text: str) -> bool:
    lowered = text.lower()
    for match in re.finditer(r"temp(?:orary)? table", lowered):
        run_up = lowered[max(0, match.start() - _LOOKBEHIND) : match.start()]
        if not any(word in run_up for word in _NEGATIONS):
            return False
    return True


def test_the_negation_check_itself_distinguishes_the_two_cases():
    assert _mentions_are_all_negated("Rewrite as a CTE, not a TEMP TABLE.")
    assert not _mentions_are_all_negated("Use CREATE TEMP TABLE or a CTE in Postgres.")


def test_nothing_recommends_a_postgres_temp_table():
    """Every surface that carries this advice, checked in one place.

    `TEMP TABLE` may still be *mentioned* — saying "not a TEMP TABLE" is the point —
    so each mention has to be negated rather than merely absent.
    """
    body = """
    CREATE TABLE #stage (id int);
    INSERT INTO #stage SELECT id FROM dbo.Orders;
    DECLARE @ids TABLE (id int);
    INSERT INTO @ids SELECT id FROM #stage;
    SELECT * FROM @ids;
    SELECT count(*) FROM @ids;
    SELECT * FROM ##shared;
    """
    texts = [
        *(r for r in temp_objects.REWRITES.values()),
        *(f.recommendation for f in check_temp_objects([], [_proc(body)])),
        *(
            rule.postgres
            for rule in bundle_rules.rewrite_rules({}, set())
            if "temp" in rule.tsql.lower() or "@t TABLE" in rule.tsql
        ),
        ai_translator._system_prompt(),
        ai_translator._build_user_prompt(_proc(body)),
    ]
    assert len(texts) > 8  # the sources above all produced something
    for text in texts:
        assert _mentions_are_all_negated(text), text


def test_a_name_only_mentioned_in_a_string_is_not_a_collection():
    assert find_temp_objects("PRINT 'now filling #stage'; SELECT 1") == []


def test_a_collection_partly_built_in_dynamic_sql_is_still_reported():
    body = "EXEC('INSERT INTO #stage SELECT 1'); SELECT * FROM #stage"
    obj = find_temp_objects(body)[0]
    assert obj.name == "#stage" and obj.writes == 1 and obj.in_dynamic_sql


def test_a_shared_reason_is_stated_once_for_the_group():
    body = """
    INSERT INTO #a SELECT id FROM dbo.Orders; SELECT * FROM #a;
    INSERT INTO #b SELECT id FROM dbo.Orders; SELECT * FROM #b;
    """
    detail = check_temp_objects([], [_proc(body)])[0].detail
    assert detail.startswith("#a, #b — filled once and read by one statement.")
