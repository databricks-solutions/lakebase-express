"""A routine that changes kind: dropping the stale one, and reporting it if it survives.

`CREATE OR REPLACE` replaces a routine of the same kind only. When a translation
reshapes a procedure into a function, re-applying it to a target that still holds the
procedure either fails (42P13, same signature) or leaves **both** (different
signature) — and then PostgreSQL picks between them by argument type, so a caller can
reach the stale one and get 42809 on every request.
"""
from backend.context_bundle.builder import build_bundle
from backend.migration.executor import _item_sql
from backend.migration.models import ObjectKind, PlanItem
from backend.schema_migration.routine_sql import kind_change_preamble, with_kind_guard

FUNCTION_SQL = (
    'CREATE OR REPLACE FUNCTION "public"."usp_ItemReport"(p_category text)\n'
    "RETURNS TABLE(item_id int) LANGUAGE plpgsql AS $$ BEGIN RETURN QUERY SELECT 1; END $$;"
)
PROCEDURE_SQL = (
    "CREATE OR REPLACE PROCEDURE public.usp_set_status(p_id int) "
    "LANGUAGE plpgsql AS $$ BEGIN END $$;"
)


# --- The preamble ------------------------------------------------------------------


def test_a_procedure_reshaped_into_a_function_drops_the_stale_procedure():
    sql = kind_change_preamble("procedure", FUNCTION_SQL)
    assert "p.prokind = 'p'" in sql          # the kind being replaced
    assert "DROP PROCEDURE" in sql
    assert "p.proname = 'usp_ItemReport'" in sql   # quoted in the DDL, so case kept
    assert "n.nspname = 'public'" in sql
    assert "CASCADE" not in sql              # a dependency must fail loudly


def test_an_unquoted_name_is_matched_folded():
    sql = kind_change_preamble(
        "procedure", "CREATE OR REPLACE FUNCTION public.Usp_Mixed() RETURNS int AS $$ $$;"
    )
    assert "p.proname = 'usp_mixed'" in sql


def test_a_missing_schema_qualifier_falls_back_to_the_target_schema():
    sql = kind_change_preamble(
        "procedure", "CREATE OR REPLACE FUNCTION plain() RETURNS int AS $$ $$;",
        default_schema="app",
    )
    assert "n.nspname = 'app'" in sql


def test_no_preamble_when_the_kind_is_unchanged():
    assert kind_change_preamble("procedure", PROCEDURE_SQL) == ""
    assert kind_change_preamble(
        "function", "CREATE FUNCTION public.f() RETURNS int AS $$ $$;"
    ) == ""


def test_no_preamble_for_views_or_triggers():
    assert kind_change_preamble("view", "CREATE OR REPLACE VIEW public.v AS SELECT 1;") == ""
    assert kind_change_preamble(
        "trigger",
        "CREATE OR REPLACE FUNCTION public.trg_fn() RETURNS trigger AS $$ $$;\n"
        "CREATE OR REPLACE TRIGGER trg AFTER INSERT ON public.t "
        "FOR EACH ROW EXECUTE FUNCTION public.trg_fn();",
    ) == ""


def test_with_kind_guard_keeps_the_original_sql_intact():
    guarded = with_kind_guard("procedure", FUNCTION_SQL)
    assert FUNCTION_SQL in guarded
    assert guarded.index("DO $lbx_kind$") < guarded.index("CREATE OR REPLACE FUNCTION")


def test_with_kind_guard_is_a_no_op_without_a_kind_change():
    assert with_kind_guard("procedure", PROCEDURE_SQL) == PROCEDURE_SQL


# --- Applied at apply time, not only at translation time ---------------------------


def test_the_executor_guards_a_stale_plan():
    """A plan built before this existed must still apply cleanly, which is why the
    guard is added where the SQL runs rather than where it was produced."""
    item = PlanItem(id="procedure:dbo.usp_ItemReport", kind=ObjectKind.PROCEDURE,
                    name="public.usp_ItemReport", sql=FUNCTION_SQL)
    assert "DROP PROCEDURE" in _item_sql(item)


def test_the_executor_leaves_an_ordinary_procedure_alone():
    item = PlanItem(id="procedure:dbo.usp_SetStatus", kind=ObjectKind.PROCEDURE,
                    name="public.usp_set_status", sql=PROCEDURE_SQL)
    assert _item_sql(item) == PROCEDURE_SQL


def test_the_async_notebook_guards_too():
    from backend.data_migration.etl_generator import _post_load_rows
    from backend.data_migration.models import PostLoadStatement

    rendered = _post_load_rows([
        PostLoadStatement(name="public.usp_ItemReport", kind="procedure", sql=FUNCTION_SQL),
    ])
    assert "DROP PROCEDURE" in rendered


# --- Reported when both routines already exist -------------------------------------


def _report():
    from backend.assessment.models import AssessmentReport

    return AssessmentReport(
        database="db", table_count=0, total_rows=0, programmable_object_count=1,
        findings=[], readiness_score=100,
        severity_counts={"info": 0, "low": 0, "medium": 0, "high": 0},
        tables=[], programmable_objects=[],
    )


def _project_with_validation():
    from backend.projects.models import Project

    plan = [{
        "id": "procedure:dbo.usp_ItemReport", "kind": "procedure",
        "name": "public.usp_ItemReport", "sql": FUNCTION_SQL,
        "original": "CREATE PROCEDURE dbo.usp_ItemReport AS BEGIN SELECT * FROM dbo.Items; END",
        "reasoning": "reshaped", "notes": "",
    }]
    # Validation still finds a *procedure* of that name in the target: the reshaped
    # function did not replace it, so both exist.
    validation = {
        "source_database": "SourceDB",
        "target_database": "databricks_postgres",
        "target_schema": "public",
        "items": [{
            "id": "procedure:dbo.usp_ItemReport", "kind": "procedure",
            "source_name": "dbo.usp_ItemReport", "target_name": "public.usp_ItemReport",
            # Validation found a *procedure* while the plan created a function.
            "target_kind": "procedure",
            "status": "matched", "severity": "info",
        }],
    }
    return Project(
        id="11111111-1111-4111-8111-111111111111", name="p",
        created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
        assessment=_report().model_dump(mode="json"), plan=plan, validation=validation,
    )


def test_the_bundle_refuses_to_promise_a_call_form_when_both_routines_exist():
    """The plan records intent; only validation knows what is really there."""
    bundle = build_bundle(_project_with_validation())

    call = bundle.callables[0]
    assert call.kind_conflict is True
    assert "Do not rely on a call form" in call.call_change
    assert "42809" in call.call_change


def test_the_skill_cautions_about_the_duplicate_rather_than_choosing():
    from backend.context_bundle.skill import render_skill

    text = render_skill(build_bundle(_project_with_validation()))
    assert "[!CAUTION]" in text
    assert "exist in the target **twice**" in text
    assert "`dbo.usp_ItemReport`" in text


# --- The generated block is not escapable -------------------------------------------
#
# Identifiers reach this from translated SQL — model output, derived from source object
# names, and editable in the plan editor — so they are input, not a trusted constant.


def _tag_of(sql: str) -> str:
    import re
    return re.search(r"DO \$(\w+)\$", sql).group(1)


def _proname_literal(sql: str) -> str:
    import re
    return re.search(r"p\.proname = (.*)$", sql, re.M).group(1)


def test_a_quote_in_a_name_is_escaped_not_closed():
    out = kind_change_preamble(
        "procedure", 'CREATE FUNCTION public."ev\'il"() RETURNS TABLE(a int) AS $$ $$;')
    assert _proname_literal(out) == "'ev''il'"


def test_a_name_containing_the_dollar_tag_cannot_close_the_block():
    """Postgres identifiers may contain `$`, so a fixed tag was escapable: the body
    ended early and everything after it parsed as top-level SQL."""
    out = kind_change_preamble(
        "procedure", 'CREATE FUNCTION public."a$lbx_kind$b"() RETURNS TABLE(a int) AS $$ $$;')

    tag = _tag_of(out)
    assert tag != "lbx_kind"                     # escalated away from the collision
    assert out.count(f"${tag}$") == 2            # exactly one open, one close
    assert _proname_literal(out) == "'a$lbx_kind$b'"   # and the real name is still targeted


def test_the_tag_escalates_past_several_collisions():
    out = kind_change_preamble(
        "procedure",
        'CREATE FUNCTION public."a$lbx_kind$q$lbx_kindx$b"() RETURNS TABLE(a int) AS $$ $$;')

    tag = _tag_of(out)
    assert out.count(f"${tag}$") == 2
    assert f"${tag}$" not in _proname_literal(out)


def test_a_benign_dollar_in_a_name_still_gets_its_guard():
    """`"a$b"` is a legal name and must not lose the guard to over-caution."""
    out = kind_change_preamble(
        "procedure", 'CREATE FUNCTION public."a$b"() RETURNS TABLE(a int) AS $$ $$;')
    assert _proname_literal(out) == "'a$b'"


def test_a_mangled_identifier_skips_the_guard_rather_than_guessing():
    """Blanking `--` comments truncates a quoted identifier containing one. Emitting a
    DROP at whatever survived would aim at a name that was never there; skipping leaves
    the re-apply to fail loudly with 42P13 instead."""
    assert kind_change_preamble(
        "procedure", 'CREATE FUNCTION public."a\nb"() RETURNS TABLE(a int) AS $$ $$;') == ""


def test_every_emitted_block_is_balanced():
    for name in ('"plain"', '"a$b"', '"a$lbx_kind$b"', '"ev\'il"', "unquoted_name"):
        out = kind_change_preamble(
            "procedure", f"CREATE FUNCTION public.{name}() RETURNS TABLE(a int) AS $$ $$;")
        if not out:
            continue
        assert out.count(f"${_tag_of(out)}$") == 2, name
