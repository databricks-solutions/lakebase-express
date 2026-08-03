"""Backend logic is pure Python — these run with no Spark/Databricks present."""
from backend.assessment.compatibility import run_all_rules
from backend.assessment.models import ColumnInfo, ProgrammableObject, Severity, TableInfo
from backend.assessment.report import build_report


def _table(*types: str) -> TableInfo:
    cols = [ColumnInfo(name=f"c{i}", data_type=t) for i, t in enumerate(types)]
    return TableInfo(schema_name="dbo", table_name="t", row_count=10, column_count=len(cols), columns=cols)


def test_type_rules_flag_incompatible_types():
    findings = run_all_rules([_table("int", "uniqueidentifier", "hierarchyid")], [])
    rule_ids = {f.rule_id for f in findings}
    assert "TYPE_UNIQUEIDENTIFIER" in rule_ids  # info-level, still reported
    assert "TYPE_HIERARCHYID" in rule_ids
    assert "TYPE_INT" not in rule_ids  # clean type -> no finding
    assert any(f.severity is Severity.HIGH for f in findings)


def test_code_rules_detect_cursor_and_dynamic_sql():
    proc = ProgrammableObject(
        schema_name="dbo",
        object_name="usp_x",
        object_type="PROCEDURE",
        line_count=3,
        definition="DECLARE c CURSOR FOR SELECT 1; EXEC('SELECT * FROM t');",
    )
    findings = run_all_rules([], [proc])
    ids = {f.rule_id for f in findings}
    assert {"CURSOR", "DYNAMIC_SQL"} <= ids


def test_inserted_deleted_only_flags_triggers():
    body = "SELECT * FROM INSERTED"
    trigger = ProgrammableObject(schema_name="dbo", object_name="trg", object_type="TRIGGER", line_count=1, definition=body)
    view = ProgrammableObject(schema_name="dbo", object_name="v", object_type="VIEW", line_count=1, definition=body)
    assert any(f.rule_id == "INSERTED_DELETED" for f in run_all_rules([], [trigger]))
    assert not any(f.rule_id == "INSERTED_DELETED" for f in run_all_rules([], [view]))


def test_readiness_score_drops_with_severity():
    high = ProgrammableObject(schema_name="dbo", object_name="p", object_type="PROCEDURE", line_count=1,
                              definition="DECLARE c CURSOR FOR SELECT 1")
    report = build_report("db", [], [high], run_all_rules([], [high]))
    assert report.readiness_score < 100
    assert report.severity_counts["high"] >= 1
