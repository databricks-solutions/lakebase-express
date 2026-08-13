"""SQL Server -> Postgres collation translation, DDL emission, and validation.

Runs without a live database: generated SQL is checked textually and parsed with
pglast (syntax only — pglast cannot know whether an ICU locale exists).
"""
import pglast
import pytest

from backend.assessment import compatibility
from backend.assessment.models import (
    CheckConstraintInfo,
    ColumnInfo,
    IndexColumnInfo,
    IndexInfo,
    Severity,
    TableInfo,
)
from backend.migration.executor import _ordered
from backend.migration.models import KIND_ORDER, ObjectKind
from backend.migration.planner import build_plan
from backend.schema_migration.collation_mapper import (
    collect_collations,
    column_collation,
    map_collation,
    parse_collation,
)
from backend.schema_migration.ddl_generator import generate_ddl, table_ddl
from backend.schema_migration.naming import IdentifierCase
from backend.validation.comparator import TargetInventory, compare
from backend.validation.models import MatchStatus

CI_AS = "SQL_Latin1_General_CP1_CI_AS"


def _text_col(name="Name", collation=CI_AS, data_type="nvarchar", length=50, nullable=False):
    return ColumnInfo(name=name, data_type=data_type, max_length=length,
                      is_nullable=nullable, collation_name=collation)


def _table(cols=None, schema="dbo", name="Customer"):
    cols = cols or [
        ColumnInfo(name="Id", data_type="int", is_nullable=False),
        _text_col(),
    ]
    return TableInfo(schema_name=schema, table_name=name, row_count=7,
                     column_count=len(cols), columns=cols)


# --- Parsing / mapping ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name,locale,deterministic",
    [
        # The Azure SQL default: case-insensitive, accent-sensitive -> ICU level2.
        (CI_AS, "und-u-ks-level2", False),
        # Case-sensitive, accent-sensitive is the full-strength comparison.
        ("Latin1_General_CS_AS", "und-u-ks-level3", True),
        # Accent-insensitive drops to level1; -kc-true adds case back for CS_AI.
        ("Latin1_General_CI_AI", "und-u-ks-level1", False),
        ("Latin1_General_CS_AI", "und-u-ks-level1-kc-true", False),
        # Version digits and the code page don't change comparison semantics.
        ("SQL_Latin1_General_CP1250_CI_AS", "und-u-ks-level2", False),
        ("Latin1_General_100_CI_AS_SC_UTF8", "und-u-ks-level2", False),
        # A language-bearing locale maps to its ICU tag.
        ("Modern_Spanish_CI_AS", "es-u-ks-level2", False),
        ("Polish_CS_AS", "pl-u-ks-level3", True),
    ],
)
def test_strength_and_locale_map_to_icu(name, locale, deterministic):
    target = map_collation(name)
    assert target.locale == locale
    assert target.deterministic is deterministic
    assert target.needs_create is True


def test_case_insensitive_collation_must_be_nondeterministic():
    """The whole point: a deterministic collation would compare bytes for equality,
    so 'ana' = 'ANA' would stay false and only sort order would change."""
    assert map_collation(CI_AS).deterministic is False
    assert map_collation("Latin1_General_CS_AS").deterministic is True


def test_binary_collation_uses_builtin_c():
    """_BIN2 is byte-order comparison, which Postgres already ships as C — so no
    CREATE COLLATION, and the reference is unqualified."""
    target = map_collation("Latin1_General_BIN2")
    assert target.name == "C"
    assert target.needs_create is False
    assert target.deterministic is True
    assert target.ddl("public") == ""
    assert target.qualified("public") == '"C"'


def test_locale_subtag_is_not_duplicated():
    """A base tag that already carries -u- must not get a second one, or the ICU
    locale is malformed."""
    target = map_collation("Chinese_Taiwan_Stroke_CI_AS")
    assert target.locale == "zh-Hant-u-co-stroke-ks-level2"
    assert target.locale.count("-u-") == 1


def test_unknown_locale_falls_back_to_root_and_is_flagged():
    """An unrecognised locale still gets the right strength — emitting nothing would
    fall back to the target's case-sensitive default."""
    target = map_collation("Klingon_Imperial_CI_AS")
    assert target.locale == "und-u-ks-level2"
    assert target.deterministic is False
    assert target.locale_fallback is True


def test_unparseable_collation_declines():
    assert map_collation("") is None
    assert map_collation("not_a_collation") is None
    assert parse_collation("Latin1_General_CI_AS").case_insensitive is True


def test_pref_token_becomes_icu_uppercase_first():
    """"Pref" sorts uppercase first and sits mid-name; left in the locale tokens it
    would look like an unknown locale and trigger a bogus fallback warning."""
    target = map_collation("SQL_Latin1_General_Pref_CP1_CS_AS")
    assert target.locale == "und-u-ks-level3-kf-upper"
    assert target.locale_fallback is False
    # Case-insensitive comparison has no upper/lower ordering to prefer.
    assert map_collation("SQL_Latin1_General_Pref_CP1_CI_AS").locale == "und-u-ks-level2"


def test_unmapped_comparison_flags_are_recorded():
    """KS/WS alter kana/width comparison for Japanese; we don't express them in the
    ICU locale, so they're recorded rather than silently dropped."""
    parsed = parse_collation("Japanese_XJIS_100_CI_AS_KS_WS")
    assert parsed.unmapped_flags == ("KS", "WS")


def test_strength_label_reads_naturally():
    assert map_collation(CI_AS).source.strength_label == "case-insensitive, accent-sensitive"
    assert map_collation("Latin1_General_BIN").source.strength_label == "binary (byte-order)"


def test_column_collation_ignores_non_character_columns():
    """SQL Server reports no collation for an int; a hand-built ColumnInfo might."""
    assert column_collation(ColumnInfo(name="Id", data_type="int", collation_name=CI_AS)) is None
    assert column_collation(ColumnInfo(name="Id", data_type="int")) is None
    # A column scanned before collations existed keeps the old behaviour.
    assert column_collation(ColumnInfo(name="Name", data_type="nvarchar")) is None
    assert column_collation(_text_col()) is not None


def test_collect_collations_dedupes_and_tracks_columns():
    t = _table(cols=[
        _text_col("A"), _text_col("B"),
        _text_col("C", collation="Modern_Spanish_CI_AI"),
        _text_col("D", collation="Latin1_General_BIN2"),
    ])
    usage = collect_collations([t])
    created = usage.created()
    # Two created (one per distinct ICU collation); the builtin C needs no CREATE.
    assert [c.name for c in created] == ["modern_spanish_ci_ai", "sql_latin1_general_cp1_ci_as"]
    assert usage.columns["sql_latin1_general_cp1_ci_as"] == ["dbo.Customer.A", "dbo.Customer.B"]


# --- DDL emission --------------------------------------------------------------------


def test_create_collation_ddl_is_idempotent_and_nondeterministic():
    ddl = map_collation(CI_AS).ddl("public")
    assert 'CREATE COLLATION IF NOT EXISTS "public"."sql_latin1_general_cp1_ci_as"' in ddl
    assert "provider = icu" in ddl
    assert "locale = 'und-u-ks-level2'" in ddl
    assert "deterministic = false" in ddl


def test_table_ddl_collates_text_columns_only():
    ddl = table_ddl(_table(), "public", IdentifierCase.LOWERCASE, "public")
    assert '"Name" varchar(50) COLLATE "public"."sql_latin1_general_cp1_ci_as" NOT NULL' in ddl
    # An int column gets no COLLATE.
    assert '"Id" integer NOT NULL' in ddl


def test_collate_is_schema_qualified_so_search_path_cannot_break_it():
    ddl = table_ddl(_table(schema="Sales"), "sales", IdentifierCase.LOWERCASE, "public")
    assert 'COLLATE "public"."sql_latin1_general_cp1_ci_as"' in ddl


def test_table_ddl_without_collation_schema_stays_unqualified():
    """Callers that pass no collation schema (older/simple paths) still emit a
    valid clause rather than a broken qualified name."""
    ddl = table_ddl(_table(), "public")
    assert 'COLLATE "sql_latin1_general_cp1_ci_as"' in ddl


def test_generate_ddl_creates_collations_before_tables():
    script, count = generate_ddl([_table()], "public")
    assert script.index("CREATE COLLATION") < script.index("CREATE TABLE")
    # schema + collation + table.
    assert count == 3
    pglast.parse_sql(script)


def test_generated_script_parses_with_every_collation_shape():
    t = _table(cols=[
        _text_col("Plain", collation=CI_AS),
        _text_col("Spanish", collation="Modern_Spanish_CI_AI"),
        _text_col("Binary", collation="Latin1_General_BIN2"),
        _text_col("Wide", collation=CI_AS, data_type="nvarchar", length=-1),
        _text_col("Fixed", collation=CI_AS, data_type="nchar", length=2),
        ColumnInfo(name="Id", data_type="int", is_nullable=False),
    ])
    script, _ = generate_ddl([t], "public")
    pglast.parse_sql(script)


def test_no_collation_section_when_nothing_to_collate():
    plain = TableInfo(schema_name="dbo", table_name="Nums", row_count=1, column_count=1,
                      columns=[ColumnInfo(name="Id", data_type="int")])
    script, count = generate_ddl([plain], "public")
    assert "CREATE COLLATION" not in script
    assert count == 2  # schema + table


# --- Plan items ----------------------------------------------------------------------


def test_plan_applies_collations_before_tables():
    items = build_plan([_table()], [], "public", translate=False, endpoint=None)
    kinds = [i.kind for i in _ordered(items)]
    assert kinds.index(ObjectKind.COLLATION) < kinds.index(ObjectKind.TABLE)
    assert KIND_ORDER[ObjectKind.COLLATION] < KIND_ORDER[ObjectKind.TABLE]


def test_collation_plan_item_warns_about_like():
    items = build_plan([_table()], [], "public", translate=False, endpoint=None)
    coll = next(i for i in items if i.kind is ObjectKind.COLLATION)
    assert coll.id == f"collation:{CI_AS}"
    assert coll.name == "public.sql_latin1_general_cp1_ci_as"
    assert "nondeterministic" in coll.notes
    assert "LIKE" in coll.notes
    assert "1 column(s)" in coll.notes


def test_deterministic_collation_item_has_no_like_warning():
    t = _table(cols=[_text_col("Code", collation="Latin1_General_CS_AS")])
    items = build_plan([t], [], "public", translate=False, endpoint=None)
    coll = next(i for i in items if i.kind is ObjectKind.COLLATION)
    assert "LIKE" not in coll.notes


def test_plan_table_sql_references_the_created_collation():
    items = build_plan([_table()], [], "public", translate=False, endpoint=None)
    coll = next(i for i in items if i.kind is ObjectKind.COLLATION)
    table = next(i for i in items if i.kind is ObjectKind.TABLE)
    # The name the CREATE makes is exactly the one the table COLLATEs.
    assert f'COLLATE "public"."{coll.name.split(".")[-1]}"' in table.sql


# --- Assessment findings -------------------------------------------------------------


def test_case_insensitive_collation_is_reported_once_per_table():
    t = _table(cols=[_text_col("A"), _text_col("B"), _text_col("C")])
    findings = compatibility.check_collations([t])
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "COLLATION_INSENSITIVE"
    assert f.severity is Severity.MEDIUM
    assert "A, B, C" in f.object_name
    assert "LIKE" in f.detail


def test_case_sensitive_collation_is_not_a_finding():
    t = _table(cols=[_text_col("A", collation="Latin1_General_CS_AS")])
    assert compatibility.check_collations([t]) == []


def test_unknown_locale_and_unparseable_collation_are_low_findings():
    t = _table(cols=[
        _text_col("A", collation="Klingon_Imperial_CI_AS"),
        _text_col("B", collation="weird-collation"),
    ])
    by_rule = {f.rule_id: f for f in compatibility.check_collations([t])}
    assert by_rule["COLLATION_LOCALE_FALLBACK"].severity is Severity.LOW
    assert by_rule["COLLATION_UNMAPPED"].severity is Severity.LOW
    # The unparseable one lands on the target default, which is case-sensitive.
    assert "case-sensitive" in by_rule["COLLATION_UNMAPPED"].detail


def test_like_on_case_insensitive_column_is_a_high_finding():
    """Postgres rejects LIKE against a nondeterministic collation outright, so a
    filtered index or CHECK doing it will fail to apply — predictable up front."""
    t = _table(cols=[_text_col("Email", data_type="nvarchar", length=255)])
    t.indexes = [IndexInfo(name="IX_Gmail", columns=[IndexColumnInfo(name="Email")],
                           filter_definition="([Email] like '%@gmail.com')")]
    t.check_constraints = [CheckConstraintInfo(name="CK_Email",
                                              definition="([Email] like '%@%')")]
    findings = [f for f in compatibility.check_collations([t])
                if f.rule_id == "COLLATION_PATTERN_MATCH"]
    assert len(findings) == 2
    assert all(f.severity is Severity.HIGH for f in findings)
    assert any("CK_Email" in f.object_name for f in findings)
    assert any("IX_Gmail" in f.object_name for f in findings)


def test_pattern_match_finding_only_for_insensitive_columns():
    """The same predicate on a case-SENSITIVE column is fine — its collation is
    deterministic, so LIKE works normally."""
    t = _table(cols=[_text_col("Email", collation="Latin1_General_CS_AS", length=255)])
    t.check_constraints = [CheckConstraintInfo(name="CK_Email",
                                              definition="([Email] like '%@%')")]
    assert compatibility.check_collations([t]) == []


def test_non_pattern_predicate_on_insensitive_column_is_fine():
    t = _table(cols=[_text_col("Email", length=255)])
    t.check_constraints = [CheckConstraintInfo(name="CK_Email",
                                              definition="([Email]<>'')")]
    rules = {f.rule_id for f in compatibility.check_collations([t])}
    assert "COLLATION_PATTERN_MATCH" not in rules


def test_collation_findings_reach_the_rule_engine():
    ids = {f.rule_id for f in compatibility.run_all_rules([_table()], [])}
    assert "COLLATION_INSENSITIVE" in ids


# --- Validation ----------------------------------------------------------------------


def _inv(collation="sql_latin1_general_cp1_ci_as"):
    """Target inventory for a migrated dbo.Customer, optionally with a collation."""
    key = ("public", "customer")
    inv = TargetInventory(
        schemas={"public"},
        tables={key},
        columns={key: {"Id": "integer", "Name": "character varying"}},
    )
    if collation:
        inv.column_collations[key] = {"Name": collation}
    return inv


def test_matching_collation_is_not_drift():
    rep = compare([_table()], [], _inv())
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert item.collation_drift == []
    assert item.status is MatchStatus.MATCHED


def test_dropped_collation_is_reported_as_drift():
    """The failure this catches: right type, right rows, wrong comparison."""
    rep = compare([_table()], [], _inv(collation=None))
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert item.status is MatchStatus.MISMATCH
    assert item.severity is Severity.MEDIUM
    assert len(item.collation_drift) == 1
    assert "Name" in item.collation_drift[0]
    assert "the database default" in item.collation_drift[0]
    assert "collation drift" in item.detail


def test_wrong_collation_is_reported_as_drift():
    rep = compare([_table()], [], _inv(collation="some_other_collation"))
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert "found some_other_collation" in item.collation_drift[0]


def test_collation_drift_fix_creates_and_alters():
    rep = compare([_table()], [], _inv(collation=None))
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert "CREATE COLLATION IF NOT EXISTS" in item.fix_sql
    assert 'ALTER COLUMN "Name" TYPE varchar(50) COLLATE' in item.fix_sql
    # The fix must be runnable as-is.
    pglast.parse_sql(item.fix_sql)


def test_missing_table_fix_carries_collations():
    rep = compare([_table()], [], TargetInventory(schemas={"public"}))
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert item.status is MatchStatus.MISSING
    assert 'COLLATE "public"."sql_latin1_general_cp1_ci_as"' in item.fix_sql


def test_readded_missing_column_keeps_its_collation():
    """ADD COLUMN must carry the collation: without it the column takes the
    case-sensitive default, and being *missing* rather than drifted, no collation
    finding would catch it."""
    key = ("public", "customer")
    inv = TargetInventory(
        schemas={"public"}, tables={key},
        columns={key: {"Id": "integer"}},  # "Name" never made it
    )
    rep = compare([_table()], [], inv)
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert item.columns_missing == ["Name"]
    assert 'ADD COLUMN "Name" varchar(50) COLLATE "public"."sql_latin1_general_cp1_ci_as"' in item.fix_sql
    # The collation must exist for that clause to resolve.
    assert 'CREATE COLLATION IF NOT EXISTS "public"."sql_latin1_general_cp1_ci_as"' in item.fix_sql
    assert item.fix_sql.index("CREATE COLLATION") < item.fix_sql.index("ADD COLUMN")
    pglast.parse_sql(item.fix_sql)


def test_readded_plain_column_gets_no_collate():
    key = ("public", "customer")
    inv = TargetInventory(
        schemas={"public"}, tables={key},
        columns={key: {"Name": "character varying"}},
        column_collations={key: {"Name": "sql_latin1_general_cp1_ci_as"}},
    )
    rep = compare([_table()], [], inv)
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert 'ADD COLUMN "Id" integer NOT NULL;' in item.fix_sql
    assert "COLLATE" not in item.fix_sql


def test_source_column_without_collation_makes_no_claim():
    """A column the scan captured before collations existed must not be reported as
    drift — the migration emitted no COLLATE for it either."""
    t = _table(cols=[
        ColumnInfo(name="Id", data_type="int", is_nullable=False),
        ColumnInfo(name="Name", data_type="nvarchar", max_length=50),
    ])
    rep = compare([t], [], _inv(collation=None))
    item = next(i for i in rep.items if i.kind is ObjectKind.TABLE)
    assert item.collation_drift == []
    assert item.status is MatchStatus.MATCHED
