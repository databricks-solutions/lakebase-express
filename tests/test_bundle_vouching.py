"""The bundle must not assert what it cannot vouch for.

Three defects found by reading a real exported skill, all the same shape — the
artifact stating something confidently that was not true, which is what sends an
application at an object that does not exist or does not work that way:

  * a call form for an object that was never translated;
  * a model note describing a translation that has since been replaced, contradicting
    the deterministic call site beside it;
  * "drop it if it should not exist" pointed at a helper the migration itself created.
"""
from backend.assessment.models import AssessmentReport
from backend.context_bundle.builder import build_bundle
from backend.context_bundle.models import AiNotes, AiObjectNote, sql_digest
from backend.context_bundle.skill import render_skill
from backend.migration.models import ObjectKind
from backend.projects.models import Project
from backend.validation.comparator import TargetInventory, compare
from backend.validation.models import MatchStatus, Severity

TRANSLATED = (
    "CREATE OR REPLACE FUNCTION public.usp_report(p int) RETURNS TABLE(a int) "
    "AS $$ BEGIN RETURN QUERY SELECT 1; END $$;"
)


def _report():
    return AssessmentReport(
        database="db", table_count=0, total_rows=0, programmable_object_count=0,
        findings=[], readiness_score=100,
        severity_counts={"info": 0, "low": 0, "medium": 0, "high": 0},
        tables=[], programmable_objects=[],
    )


def _project(plan, ai_notes=None):
    project = Project(
        id="11111111-1111-4111-8111-111111111111", name="p",
        created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
        assessment=_report().model_dump(mode="json"), plan=plan,
    )
    return build_bundle(project, ai_notes=ai_notes)


def _plan_item(obj_id, kind, name, sql, original="CREATE PROCEDURE x AS SELECT 1"):
    return {"id": obj_id, "kind": kind, "name": name, "sql": sql,
            "original": original, "reasoning": "r", "notes": ""}


# --- An untranslated object has no call form ---------------------------------------


def test_an_untranslated_object_is_not_given_a_call_form():
    """It is grouped under its source kind, but nothing was created — naming a call
    form sends the caller at an object that does not exist (SQLSTATE 42883)."""
    bundle = _project([
        _plan_item("procedure:dbo.usp_Missing", "procedure", "public.usp_missing", ""),
    ])
    text = render_skill(bundle)

    assert "not translated — no object to call" in text
    assert "CALL public.usp_missing" not in text


def test_a_translated_object_still_gets_its_call_form():
    bundle = _project([
        _plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", TRANSLATED),
    ])
    text = render_skill(bundle)

    assert "SELECT * FROM public.usp_report(...)" in text
    assert "not translated" not in text.split("## 4.")[0].split("### Functions")[1]


# --- A note that no longer describes the translation is dropped --------------------


def _notes(source, digest):
    return AiNotes(
        endpoint="ep", success=True, objects_total=1,
        notes=[AiObjectNote(source=source, object_type="PROCEDURE", sql_digest=digest,
                            call_site="CALL it and FETCH two refcursors")],
    )


def test_a_note_matching_the_current_sql_is_kept():
    plan = [_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", TRANSLATED)]
    bundle = _project(plan, ai_notes=_notes("dbo.usp_Report", sql_digest(TRANSLATED)))

    assert bundle.ai_notes is not None and len(bundle.ai_notes.notes) == 1
    assert not any("model note" in c for c in bundle.provenance.completeness)


def test_a_note_describing_a_replaced_translation_is_dropped_and_declared():
    """The real case: a note written against a refcursor procedure, still sitting beside
    a call site that now says SELECT * FROM — two incompatible call forms again."""
    plan = [_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", TRANSLATED)]
    bundle = _project(plan, ai_notes=_notes("dbo.usp_Report", sql_digest("SOMETHING ELSE")))

    assert bundle.ai_notes is not None and bundle.ai_notes.notes == []
    assert any("has since been replaced" in c for c in bundle.provenance.completeness)
    assert "FETCH two refcursors" not in render_skill(bundle)


def test_a_note_for_an_object_no_longer_translated_is_dropped():
    plan = [_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", "")]
    bundle = _project(plan, ai_notes=_notes("dbo.usp_Report", sql_digest(TRANSLATED)))

    assert bundle.ai_notes.notes == []


def test_a_note_without_a_digest_is_kept_as_unverifiable():
    """Notes stored before digests existed are not known-stale, and emptying the section
    for every existing project would trade one wrong impression for another."""
    plan = [_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", TRANSLATED)]
    bundle = _project(plan, ai_notes=_notes("dbo.usp_Report", ""))

    assert len(bundle.ai_notes.notes) == 1
    assert not any("model note" in c for c in bundle.provenance.completeness)


def test_digest_ignores_surrounding_whitespace_only():
    assert sql_digest(f"  {TRANSLATED}\n") == sql_digest(TRANSLATED)
    assert sql_digest(TRANSLATED.replace("SELECT 1", "SELECT 2")) != sql_digest(TRANSLATED)


# --- A helper the migration created is not offered for deletion --------------------


def _compare(objects, **inv):
    from backend.assessment.models import ProgrammableObject  # noqa: F401
    inv.setdefault("schemas", {"public"})
    return compare([], objects, TargetInventory(**inv), include_tables=True)


def _proc(name):
    from backend.assessment.models import ProgrammableObject

    return ProgrammableObject(
        schema_name="dbo", object_name=name, object_type="PROCEDURE", line_count=1,
        definition="CREATE PROCEDURE x AS BEGIN SELECT 1 FROM dbo.T; END",
    )


def test_a_working_table_named_after_its_owner_is_not_offered_for_deletion():
    """A temp-table rewrite creates `<proc>_Scratch`; the procedure depends on it, so
    "use Remove from target to drop it" would break the object that owns it."""
    report = _compare([_proc("usp_ItemReport")],
                      functions={("public", "usp_itemreport")},
                      tables={("public", "usp_itemreport_scratch")})

    helper = next(i for i in report.items if "scratch" in i.target_name)
    assert helper.status is MatchStatus.EXTRA
    assert helper.severity is Severity.INFO          # not LOW-with-a-drop-button
    assert "helper the migration created" in helper.detail
    assert "public.usp_itemreport" in helper.detail
    assert "Leave it in place" in helper.recommendation
    assert not helper.fix_sql                        # no destructive one-click


def test_per_result_set_helper_functions_are_recognised():
    report = _compare([_proc("usp_OrderSummary")],
                      functions={("public", "usp_ordersummary"),
                                 ("public", "usp_ordersummary_header"),
                                 ("public", "usp_ordersummary_lines")})

    helpers = [i for i in report.items if i.status is MatchStatus.EXTRA]
    assert {i.target_name for i in helpers} == {
        "public.usp_ordersummary_header", "public.usp_ordersummary_lines",
    }
    assert all(i.severity is Severity.INFO for i in helpers)


def test_an_unrelated_object_is_still_reported_as_extra():
    """The suffix separator matters: `OrdersArchive` is not a helper of `Orders`."""
    report = _compare([_proc("usp_Report")],
                      functions={("public", "usp_report")},
                      tables={("public", "usp_reportarchive"), ("public", "leftover")})

    extras = {i.target_name: i for i in report.items if i.status is MatchStatus.EXTRA}
    assert set(extras) == {"public.usp_reportarchive", "public.leftover"}
    for item in extras.values():
        assert item.severity is Severity.LOW
        assert "Remove from target" in item.recommendation


def test_the_longest_owner_wins():
    report = _compare([_proc("usp_Get"), _proc("usp_GetOrder")],
                      functions={("public", "usp_get"), ("public", "usp_getorder"),
                                 ("public", "usp_getorder_lines")})

    helper = next(i for i in report.items if i.status is MatchStatus.EXTRA)
    assert "public.usp_getorder" in helper.detail
    assert "public.usp_get\"" not in helper.detail


# --- Notes say when they were written, and what was dropped ------------------------


def test_notes_carry_their_age_into_the_skill():
    """They are generated once and replayed on every export, so "when" is the only
    thing separating current advice from advice about a replaced translation."""
    plan = [_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", TRANSLATED)]
    notes = _notes("dbo.usp_Report", sql_digest(TRANSLATED))
    notes = notes.model_copy(update={"generated_at": "2026-09-01T10:00:00+00:00"})
    text = render_skill(_project(plan, ai_notes=notes))

    assert "written by `ep` on 2026-09-01T10:00:00+00:00" in text


def test_the_skill_prompts_a_re_run_when_notes_were_dropped():
    plan = [_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", TRANSLATED)]
    bundle = _project(plan, ai_notes=_notes("dbo.usp_Report", sql_digest("OLD")))

    assert bundle.ai_notes.stale_dropped == 1
    text = render_skill(bundle)
    assert "1 note described a translation that has since been replaced" in text
    assert "re-run the notes" in text


def test_nothing_is_said_when_every_note_still_matches():
    plan = [_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report", TRANSLATED)]
    bundle = _project(plan, ai_notes=_notes("dbo.usp_Report", sql_digest(TRANSLATED)))

    assert bundle.ai_notes.stale_dropped == 0
    assert "has since been replaced" not in render_skill(bundle)


def test_generated_at_is_stamped_on_a_real_run(monkeypatch):
    """The generator sets it, so new notes never arrive undated."""
    import json
    from types import SimpleNamespace

    from backend.context_bundle import ai_notes as mod

    payload = {"notes": [{"source": "dbo.usp_Report", "call_site": "c",
                          "behaviour": "b", "watch_out": "w"}]}
    monkeypatch.setattr(mod, "query_chat", lambda *a, **k: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)),
                                 finish_reason="stop")]))
    project = Project(
        id="11111111-1111-4111-8111-111111111111", name="p",
        created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
        plan=[_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report",
                         TRANSLATED)],
    )
    out = mod.generate_ai_notes(project, endpoint="ep")

    assert out.success and out.generated_at
    assert out.generated_at.startswith("20")
    # And pinned to the SQL it described, so it can go stale verifiably.
    assert out.notes[0].sql_digest == sql_digest(TRANSLATED)


# --- Notes resolve back to a plan item whatever the model calls it ------------------


def test_every_source_spelling_the_model_has_produced_resolves():
    """The context labels objects `### PROCEDURE dbo.X -> public.x` and the model echoes
    that back inconsistently — bare on one run, kind-prefixed on the next. Matching the
    exact string silently lost both the object type and the digest, leaving the staleness
    check permanently inert."""
    from backend.context_bundle.ai_notes import _resolve, _source_of
    from backend.migration.models import PlanItem

    items = [
        PlanItem(id="procedure:dbo.usp_Report", kind=ObjectKind.PROCEDURE,
                 name="public.usp_report", sql=TRANSLATED),
        PlanItem(id="view:dbo.vw_Totals", kind=ObjectKind.VIEW,
                 name="public.vw_totals", sql="CREATE VIEW public.vw_totals AS SELECT 1;"),
    ]
    by_source = {_source_of(i): i for i in items}

    for spelling in (
        "dbo.usp_Report",                            # bare
        "PROCEDURE dbo.usp_Report",                  # kind-prefixed
        "procedure dbo.usp_report",                  # lower-cased
        "dbo.usp_Report  ->  public.usp_report",     # the whole heading
        "`dbo.usp_Report`",                          # backticked
        "usp_Report",                                # schema dropped
    ):
        assert _resolve(spelling, by_source) is items[0], spelling

    assert _resolve("VIEW dbo.vw_Totals", by_source) is items[1]
    assert _resolve("dbo.usp_NotInThePlan", by_source) is None
    assert _resolve("", by_source) is None


def test_an_unattributable_note_is_dropped_rather_than_kept_undigested():
    """A note that cannot be tied to a plan item can never be checked for staleness, so
    keeping it would reintroduce exactly the contradiction this guards against."""
    import json
    from types import SimpleNamespace

    from backend.context_bundle import ai_notes as mod

    payload = {"notes": [
        {"source": "PROCEDURE dbo.usp_Report", "call_site": "kept", "behaviour": "",
         "watch_out": ""},
        {"source": "dbo.usp_Vanished", "call_site": "dropped", "behaviour": "",
         "watch_out": ""},
    ]}
    monkey = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")])
    import pytest
    with pytest.MonkeyPatch().context() as m:
        m.setattr(mod, "query_chat", lambda *a, **k: monkey)
        out = mod.generate_ai_notes(Project(
            id="11111111-1111-4111-8111-111111111111", name="p",
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
            plan=[_plan_item("procedure:dbo.usp_Report", "procedure", "public.usp_report",
                             TRANSLATED)],
        ), endpoint="ep")

    assert [n.call_site for n in out.notes] == ["kept"]
    note = out.notes[0]
    assert note.source == "dbo.usp_Report"          # the plan's spelling, not the model's
    assert note.object_type == "PROCEDURE"          # the lookup that used to fall through
    assert note.sql_digest == sql_digest(TRANSLATED)
