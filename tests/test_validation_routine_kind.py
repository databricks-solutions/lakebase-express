"""Validation verifies the kind a code object is *supposed* to have.

Reshaping a row-returning procedure into a function is the normal path, not an
exception, so validation derives the expected kind from the source instead of
assuming it equals the source kind. That has to hold in four states: correctly
reshaped, still a procedure, a refcursor procedure, and absent — and the clean case
must be silent, or every migration would report a false missing procedure plus a
false extra function.
"""
from backend.assessment.models import ProgrammableObject
from backend.migration.models import ObjectKind
from backend.validation.comparator import TargetInventory, compare
from backend.validation.models import MatchStatus, Severity

ROW_RETURNING = (
    "CREATE PROCEDURE dbo.usp_ItemReport @Category NVARCHAR(64) AS\n"
    "BEGIN\n"
    "    SELECT ItemId, Total FROM dbo.Items WHERE Category = @Category ORDER BY Total DESC;\n"
    "END"
)
WRITE_ONLY = (
    "CREATE PROCEDURE dbo.usp_SetStatus @Id INT AS\n"
    "BEGIN\n"
    "    UPDATE dbo.Orders SET Status = 1 WHERE OrderId = @Id;\n"
    "END"
)


def _proc(name: str, definition: str) -> ProgrammableObject:
    return ProgrammableObject(schema_name="dbo", object_name=name, object_type="PROCEDURE",
                              line_count=4, definition=definition)


def _report(objects, **inv):
    inv.setdefault("schemas", {"public"})
    return compare([], objects, TargetInventory(**inv), include_tables=False)


def _item(report, source_name):
    return next(i for i in report.items if i.source_name == source_name)


# --- The clean case must be silent -------------------------------------------------


def test_a_reshaped_procedure_matches_as_a_function():
    report = _report([_proc("usp_ItemReport", ROW_RETURNING)],
                     functions={("public", "usp_itemreport")})

    item = _item(report, "dbo.usp_ItemReport")
    assert item.status is MatchStatus.MATCHED
    assert item.target_kind == "function"
    assert "returns a result set" in item.detail
    assert "SELECT * FROM" in item.detail


def test_the_reshaped_function_is_not_also_reported_as_extra():
    """The false pair this mechanism exists to prevent: a missing procedure and an
    extra function for one correctly migrated object."""
    report = _report([_proc("usp_ItemReport", ROW_RETURNING)],
                     functions={("public", "usp_itemreport")})

    assert not any(i.status is MatchStatus.EXTRA for i in report.items)
    assert not any(i.status is MatchStatus.MISSING for i in report.items)


def test_a_write_only_procedure_still_expects_a_procedure():
    report = _report([_proc("usp_SetStatus", WRITE_ONLY)],
                     procedures={("public", "usp_setstatus")})

    item = _item(report, "dbo.usp_SetStatus")
    assert item.status is MatchStatus.MATCHED
    assert item.target_kind == "procedure"
    assert item.detail == ""          # nothing changed, so nothing to say


# --- Still a procedure: the failure the application cannot work around -------------


def test_a_row_returning_procedure_left_as_a_procedure_is_a_high_mismatch():
    report = _report([_proc("usp_ItemReport", ROW_RETURNING)],
                     procedures={("public", "usp_itemreport")})

    item = _item(report, "dbo.usp_ItemReport")
    assert item.status is MatchStatus.MISMATCH
    assert item.severity is Severity.HIGH
    assert item.target_kind == "procedure"
    assert "42809" in item.detail
    assert "RETURNS TABLE" in item.recommendation
    # Carries the source so the existing AI fix / repair loop can re-translate it.
    assert item.source_definition == ROW_RETURNING


def test_a_refcursor_procedure_is_accepted_as_a_working_shape():
    """Rows can come back through an INOUT refcursor the caller FETCHes, so this is
    not the wrong kind — reporting it as one would be a false alarm."""
    report = _report(
        [_proc("usp_ItemReport", ROW_RETURNING)],
        procedures={("public", "usp_itemreport")},
        refcursor_routines={("public", "usp_itemreport")},
    )

    item = _item(report, "dbo.usp_ItemReport")
    assert item.status is MatchStatus.MATCHED
    assert item.target_kind == "procedure"
    assert "FETCH" in item.detail


# --- Both routines present, and absent ---------------------------------------------


def test_both_kinds_present_matches_the_function_and_flags_the_procedure():
    report = _report(
        [_proc("usp_ItemReport", ROW_RETURNING)],
        procedures={("public", "usp_itemreport")},
        functions={("public", "usp_itemreport")},
    )

    matched = _item(report, "dbo.usp_ItemReport")
    assert matched.status is MatchStatus.MATCHED and matched.target_kind == "function"

    collision = next(i for i in report.items if i.status is MatchStatus.EXTRA)
    assert collision.severity is Severity.HIGH
    assert "Both a procedure and a function" in collision.detail
    assert "42809" in collision.detail


def test_a_missing_reshaped_procedure_says_which_kind_it_needs():
    report = _report([_proc("usp_ItemReport", ROW_RETURNING)])

    item = _item(report, "dbo.usp_ItemReport")
    assert item.status is MatchStatus.MISSING
    assert item.severity is Severity.HIGH
    assert item.target_kind == ""
    assert "Function" in item.detail
    assert "must be a function here" in item.detail


def test_a_missing_ordinary_procedure_reads_as_before():
    report = _report([_proc("usp_SetStatus", WRITE_ONLY)])

    item = _item(report, "dbo.usp_SetStatus")
    assert item.status is MatchStatus.MISSING
    assert "Procedure" in item.detail
    assert "must be a function" not in item.detail


# --- Unaffected kinds --------------------------------------------------------------


def test_views_and_triggers_are_untouched_by_the_kind_rule():
    objects = [
        ProgrammableObject(schema_name="dbo", object_name="vw_Items", object_type="VIEW",
                           line_count=1, definition="SELECT * FROM dbo.Items"),
        ProgrammableObject(schema_name="dbo", object_name="trg_Items", object_type="TRIGGER",
                           line_count=1, definition="SELECT * FROM INSERTED"),
    ]
    report = compare([], objects, TargetInventory(
        schemas={"public"},
        views={("public", "vw_items")},
        triggers={("public", "trg_items")},
        functions={("public", "trg_items_fn")},
    ), include_tables=False)

    by_kind = {i.kind: i for i in report.items if i.source_name}
    assert by_kind[ObjectKind.VIEW].status is MatchStatus.MATCHED
    assert by_kind[ObjectKind.VIEW].target_kind == "view"
    assert by_kind[ObjectKind.TRIGGER].status is MatchStatus.MATCHED
    assert not any(i.status is MatchStatus.EXTRA for i in report.items)


# --- The plan as a second source of truth ------------------------------------------
#
# Two things nothing else can supply: whether the translation returns a set (the T-SQL
# text scan misses a CTE-fronted result set, a body without semicolons, and rows coming
# from EXEC-ing another procedure), and which target objects the migration created as
# helpers (naming is not stable between translations).

from backend.migration.models import ObjectKind, PlanItem  # noqa: E402

SET_RETURNING = ("CREATE OR REPLACE FUNCTION public.usp_itemreport(p text) "
                 "RETURNS TABLE(a int) LANGUAGE plpgsql AS $$ BEGIN RETURN QUERY "
                 "SELECT 1; END $$;")
# A result set the source-side heuristic cannot see.
CTE_SOURCE = ("CREATE PROCEDURE dbo.usp_ItemReport AS BEGIN "
              "WITH c AS (SELECT a FROM dbo.T) SELECT * FROM c; END")


def _plan(sql, source="dbo.usp_ItemReport", name="public.usp_itemreport"):
    return [PlanItem(id=f"procedure:{source}", kind=ObjectKind.PROCEDURE, name=name, sql=sql)]


def test_the_source_heuristic_alone_misses_a_cte_result_set():
    """Documents the gap the plan signal closes — if this ever starts passing, the
    heuristic improved and the union is belt-and-braces rather than load-bearing."""
    from backend.assessment import callable_shape as cs

    assert cs.returns_result_set(CTE_SOURCE) is False


def test_the_plan_makes_validation_expect_a_function_anyway():
    obj = _proc("usp_ItemReport", CTE_SOURCE)
    inv = TargetInventory(schemas={"public"}, functions={("public", "usp_itemreport")})

    without = compare([], [obj], inv, include_tables=False)
    with_plan = compare([], [obj], inv, include_tables=False, plan=_plan(SET_RETURNING))

    # Without the plan the source scan saw no rows, so a function reads as the wrong
    # kind: a missing procedure plus an unexplained extra function.
    assert _item(without, "dbo.usp_ItemReport").status is MatchStatus.MISSING
    assert any(i.status is MatchStatus.EXTRA for i in without.items)

    # With it, the object is correctly matched as a reshaped function and nothing is extra.
    matched = _item(with_plan, "dbo.usp_ItemReport")
    assert matched.status is MatchStatus.MATCHED
    assert matched.target_kind == "function"
    assert not any(i.status is MatchStatus.EXTRA for i in with_plan.items)


def test_a_plan_only_signal_does_not_claim_the_source_returns_rows():
    """The explanation must not assert what only the translation indicated."""
    obj = _proc("usp_ItemReport", CTE_SOURCE)
    rep = compare([], [obj], TargetInventory(schemas={"public"},
                  functions={("public", "usp_itemreport")}),
                  include_tables=False, plan=_plan(SET_RETURNING))

    detail = _item(rep, "dbo.usp_ItemReport").detail
    assert "did not itself find a result set" in detail
    assert "worth confirming" in detail


def test_both_signals_agreeing_gives_the_plain_explanation():
    obj = _proc("usp_ItemReport", ROW_RETURNING)
    rep = compare([], [obj], TargetInventory(schemas={"public"},
                  functions={("public", "usp_itemreport")}),
                  include_tables=False, plan=_plan(SET_RETURNING))

    detail = _item(rep, "dbo.usp_ItemReport").detail
    assert "returns a result set" in detail
    assert "did not itself find" not in detail


def test_a_write_only_procedure_is_untouched_by_the_plan_signal():
    obj = _proc("usp_SetStatus", WRITE_ONLY)
    plan = _plan("CREATE OR REPLACE PROCEDURE public.usp_setstatus() AS $$ BEGIN END $$;",
                 source="dbo.usp_SetStatus", name="public.usp_setstatus")
    rep = compare([], [obj], TargetInventory(schemas={"public"},
                  procedures={("public", "usp_setstatus")}), include_tables=False, plan=plan)

    item = _item(rep, "dbo.usp_SetStatus")
    assert item.status is MatchStatus.MATCHED and item.target_kind == "procedure"
    assert item.detail == ""


def test_a_helper_is_recognised_whatever_the_translation_named_it():
    """The naming that defeated name-matching: no owner prefix at all."""
    sql = ('CREATE UNLOGGED TABLE public."ReorderScratch" (run_id uuid);\n' + SET_RETURNING)
    obj = _proc("usp_ItemReport", ROW_RETURNING)
    inv = TargetInventory(schemas={"public"}, functions={("public", "usp_itemreport")},
                          tables={("public", "ReorderScratch")})

    without = compare([], [obj], inv, include_tables=True)
    with_plan = compare([], [obj], inv, include_tables=True, plan=_plan(sql))

    loose = next(i for i in without.items if i.status is MatchStatus.EXTRA)
    assert loose.severity is Severity.LOW and loose.fix_sql        # offers a DROP

    exact = next(i for i in with_plan.items if i.status is MatchStatus.EXTRA)
    assert exact.severity is Severity.INFO and not exact.fix_sql   # no DROP
    assert "public.usp_itemreport" in exact.detail


def test_an_object_the_migration_did_not_create_is_still_extra():
    obj = _proc("usp_ItemReport", ROW_RETURNING)
    rep = compare([], [obj], TargetInventory(schemas={"public"},
                  functions={("public", "usp_itemreport")},
                  tables={("public", "someone_elses_table")}),
                  include_tables=True, plan=_plan(SET_RETURNING))

    extra = next(i for i in rep.items if i.status is MatchStatus.EXTRA)
    assert extra.target_name == "public.someone_elses_table"
    assert extra.severity is Severity.LOW and extra.fix_sql


# --- The plan is loaded from the project, not sent by the caller --------------------


def test_the_runner_loads_the_plan_for_the_project_being_validated():
    """Both plan-derived signals are silent no-ops if this returns nothing, so a
    regression here would look like the signals themselves not working."""
    from unittest.mock import patch

    from backend.projects.models import Project
    from backend.validation import runs

    project = Project(
        id="p1", name="p", created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        plan=[{"id": "procedure:dbo.usp_X", "kind": "procedure",
               "name": "public.usp_x", "sql": SET_RETURNING, "original": "x"}],
    )

    class _Store:
        def get(self, pid):
            return project if pid == "p1" else None

    with patch.object(runs, "get_store", lambda: _Store()):
        assert len(runs._stored_plan("p1")) == 1
        assert runs._stored_plan("unknown") == []
        assert runs._stored_plan("") == []
        assert runs._stored_plan(None) == []


def test_a_store_failure_degrades_instead_of_failing_the_run():
    from unittest.mock import patch

    from backend.validation import runs

    class _Broken:
        def get(self, pid):
            raise RuntimeError("store unreachable")

    with patch.object(runs, "get_store", lambda: _Broken()):
        assert runs._stored_plan("p1") == []
