"""The one model-written part of the export: labelled, opt-in, and fail-soft."""
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import context_routes
from backend.context_bundle import ai_notes as ai_notes_module
from backend.context_bundle.ai_notes import code_items, generate_ai_notes
from backend.context_bundle.builder import build_bundle
from backend.context_bundle.models import AiNotes, AiNotesRunState, AiObjectNote
from backend.context_bundle.skill import render_skill
from backend.migration.models import ObjectKind
from backend.projects.store import LocalFileStore
from tests.test_context_bundle import _UUID, _col, _plan_item, _project, _report, _table

ENDPOINT = "databricks-claude-opus-4-8"


def _chat_response(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason,
        )]
    )


def _must_not_be_called():
    raise AssertionError("the model must not be called when there is nothing to read")


def _translated_plan() -> list[dict]:
    return [
        _plan_item("procedure:dbo.usp_PlaceOrder", ObjectKind.PROCEDURE,
                   "public.usp_PlaceOrder", "CREATE OR REPLACE PROCEDURE ...",
                   original="CREATE PROCEDURE dbo.usp_PlaceOrder @Id int AS SELECT 1",
                   reasoning="translated"),
        # No target SQL: nothing to compare, so it is not sent to the model.
        _plan_item("function:dbo.fn_Untranslated", ObjectKind.FUNCTION,
                   "public.fn_untranslated", "", original="CREATE FUNCTION ..."),
        # A table is not a code object.
        _plan_item("table:dbo.Orders", ObjectKind.TABLE, "public.orders", "CREATE TABLE ..."),
    ]


# --- What gets sent -----------------------------------------------------------------


def test_only_translated_code_objects_are_sent():
    project = _project(report=_report(), plan=_translated_plan())

    assert [i.id for i in code_items(project)] == ["procedure:dbo.usp_PlaceOrder"]


def test_the_prompt_carries_both_sides_and_is_bounded(monkeypatch):
    """The model's job needs the original beside its translation; the body is capped
    so the prompt is the same size whatever the source looks like."""
    captured = {}

    def fake_query(endpoint, messages, **params):
        captured["endpoint"] = endpoint
        captured["system"] = messages[0].content
        captured["user"] = messages[1].content
        captured["params"] = params
        return _chat_response('{"notes": []}')

    monkeypatch.setattr(ai_notes_module, "query_chat", fake_query)
    monkeypatch.setattr(ai_notes_module, "chat_text", lambda resp: resp.choices[0].message.content)

    long_body = "SELECT " + "x" * 5000
    plan = [_plan_item("procedure:dbo.Big", ObjectKind.PROCEDURE, "public.big",
                       "CREATE PROCEDURE ...", original=long_body)]
    result = generate_ai_notes(_project(report=_report(), plan=plan), ENDPOINT)

    assert result.success is True
    assert captured["endpoint"] == ENDPOINT
    assert "ORIGINAL T-SQL:" in captured["user"]
    assert "TRANSLATED POSTGRESQL:" in captured["user"]
    assert "…truncated…" in captured["user"]
    assert len(captured["user"]) < 2 * ai_notes_module._MAX_DEF_CHARS + 2000
    # Structured output, so the answer is parseable rather than prose.
    assert captured["params"]["response_format"]["json_schema"]["name"] == "app_migration_notes"


def test_notes_are_parsed_and_typed(monkeypatch):
    project = _project(report=_report(), plan=_translated_plan())
    monkeypatch.setattr(ai_notes_module, "query_chat", lambda *a, **kw: _chat_response(
        '{"notes": [{"source": "dbo.usp_PlaceOrder", "call_site": "Now returns no rows.",'
        ' "behaviour": "Raises instead of returning -1.", "watch_out": "Callers ignore the error."}]}'
    ))
    monkeypatch.setattr(ai_notes_module, "chat_text", lambda resp: resp.choices[0].message.content)
    result = generate_ai_notes(project, ENDPOINT)

    assert result.success is True
    assert result.endpoint == ENDPOINT
    note = result.notes[0]
    assert note.source == "dbo.usp_PlaceOrder"
    assert note.object_type == "PROCEDURE"          # filled in from the plan, not the model
    assert note.call_site == "Now returns no rows."


# --- Fail-soft ----------------------------------------------------------------------


def test_a_model_error_never_breaks_the_export(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("endpoint unavailable")

    monkeypatch.setattr(ai_notes_module, "query_chat", boom)
    result = generate_ai_notes(_project(report=_report(), plan=_translated_plan()), ENDPOINT)

    assert result.success is False
    assert "endpoint unavailable" in (result.error or "")
    assert result.endpoint == ENDPOINT


def test_hitting_the_output_limit_says_so(monkeypatch):
    monkeypatch.setattr(ai_notes_module, "query_chat",
                        lambda *a, **kw: _chat_response("{", finish_reason="length"))
    result = generate_ai_notes(_project(report=_report(), plan=_translated_plan()), ENDPOINT)

    assert result.success is False
    assert "output-token limit" in (result.error or "")


def test_nothing_to_read_is_reported_without_calling_a_model(monkeypatch):
    monkeypatch.setattr(ai_notes_module, "query_chat",
                        lambda *a, **kw: _must_not_be_called())
    result = generate_ai_notes(_project(report=_report()), ENDPOINT)

    assert result.success is False
    assert "no translated procedures" in (result.error or "")


# --- How it reads in the skill ------------------------------------------------------


def _skill_with_notes(notes: AiNotes) -> str:
    bundle = build_bundle(
        _project(report=_report(tables=[_table("Orders", [_col("IsPaid", "bit")])]),
                 plan=_translated_plan()),
        ai_notes=notes,
    )
    return render_skill(bundle)


def test_the_section_names_the_model_and_says_it_is_advisory():
    text = _skill_with_notes(AiNotes(
        endpoint=ENDPOINT, success=True,
        notes=[AiObjectNote(source="dbo.usp_PlaceOrder", object_type="PROCEDURE",
                            call_site="Read the INOUT parameter instead of a result set.",
                            behaviour="Raises on a missing customer.",
                            watch_out="The old code checked for -1.")],
    ))

    assert "## 8. Model notes on the translated objects (advisory)" in text
    assert f"written by `{ENDPOINT}`" in text
    assert "Everything above this section is deterministic" in text
    assert "**At the call site:** Read the INOUT parameter" in text
    assert "**Watch out:** The old code checked for -1." in text


def test_objects_the_model_never_read_are_declared():
    """29 translated objects, 20 notes: silence must not read as "nothing to worry
    about" for the 9 the prompt cap left out."""
    text = _skill_with_notes(AiNotes(
        endpoint=ENDPOINT, success=True, objects_total=29,
        notes=[AiObjectNote(source=f"dbo.P{i}", object_type="PROCEDURE", call_site="x")
               for i in range(20)],
    ))

    assert "Covers 20 of 29 translated objects" in text
    assert '"not looked at"' in text


def test_nothing_is_declared_when_every_object_was_read():
    text = _skill_with_notes(AiNotes(
        endpoint=ENDPOINT, success=True, objects_total=1,
        notes=[AiObjectNote(source="dbo.P", object_type="PROCEDURE", call_site="x")],
    ))

    assert "translated objects — the rest" not in text


def test_the_advisory_section_comes_after_the_deterministic_ones():
    """A reader should have the facts before anything they must verify."""
    text = _skill_with_notes(AiNotes(endpoint=ENDPOINT, success=True, notes=[
        AiObjectNote(source="dbo.usp_PlaceOrder", object_type="PROCEDURE", call_site="x"),
    ]))

    assert text.index('## 7. Do not "fix" these') < text.index("## 8. Model notes")


def test_a_failed_model_pass_degrades_to_one_line():
    text = _skill_with_notes(AiNotes(endpoint=ENDPOINT, success=False, error="endpoint unavailable"))

    assert "could not be produced: endpoint unavailable" in text
    # And the deterministic sections are untouched.
    assert '## 7. Do not "fix" these' in text
    assert "## 1. Identifiers and quoting" in text


def test_without_the_model_pass_there_is_no_advisory_section():
    bundle = build_bundle(_project(report=_report(), plan=_translated_plan()))

    assert bundle.ai_notes is None
    assert "advisory" not in render_skill(bundle).lower()
    assert all(s.name != "ai_notes" for s in bundle.sections)


def test_the_section_index_marks_it_ai():
    bundle = build_bundle(
        _project(report=_report(), plan=_translated_plan()),
        ai_notes=AiNotes(endpoint=ENDPOINT, success=True, notes=[
            AiObjectNote(source="dbo.usp_PlaceOrder", object_type="PROCEDURE"),
        ]),
    )

    section = next(s for s in bundle.sections if s.name == "ai_notes")
    assert (section.count, section.provenance) == (1, "ai")


# --- Route --------------------------------------------------------------------------


def _client(monkeypatch, store) -> TestClient:
    monkeypatch.setattr(context_routes, "get_store", lambda: store)
    app = FastAPI()
    app.include_router(context_routes.router)
    return TestClient(app)


def test_the_exports_never_call_a_model_inline(tmp_path, monkeypatch):
    """Reading every object takes minutes — measured at 4m07s on a 29-object
    database — so a download must never wait on it."""
    store = LocalFileStore(str(tmp_path))
    store.save(_project(report=_report(), plan=_translated_plan()))
    monkeypatch.setattr(context_routes.runs, "latest_notes", lambda pid: None)
    monkeypatch.setattr(ai_notes_module, "query_chat", lambda *a, **kw: _must_not_be_called())
    client = _client(monkeypatch, store)

    assert client.get(f"/api/projects/{_UUID}/context-skill").status_code == 200
    assert client.get(f"/api/projects/{_UUID}/context-bundle").json()["ai_notes"] is None


def test_a_finished_run_is_carried_into_both_exports(tmp_path, monkeypatch):
    store = LocalFileStore(str(tmp_path))
    store.save(_project(report=_report(), plan=_translated_plan()))
    notes = AiNotes(endpoint=ENDPOINT, success=True, notes=[
        AiObjectNote(source="dbo.usp_PlaceOrder", object_type="PROCEDURE",
                     call_site="Read the INOUT parameter."),
    ])
    monkeypatch.setattr(context_routes.runs, "latest_notes", lambda pid: notes)
    client = _client(monkeypatch, store)

    assert client.get(f"/api/projects/{_UUID}/context-bundle").json()["ai_notes"]["endpoint"] == ENDPOINT
    assert f"written by `{ENDPOINT}`" in client.get(f"/api/projects/{_UUID}/context-skill").text


def test_the_run_starts_in_the_background_and_is_pollable(tmp_path, monkeypatch):
    store = LocalFileStore(str(tmp_path))
    store.save(_project(report=_report(), plan=_translated_plan()))
    started: list[tuple[str, str | None]] = []
    monkeypatch.setattr(context_routes.runs, "start_run",
                        lambda project, endpoint=None: started.append((project.id, endpoint)) or "run-1")
    monkeypatch.setattr(context_routes.runs, "get_run",
                        lambda run_id: AiNotesRunState(run_id=run_id, status="running",
                                                       endpoint=ENDPOINT, objects_total=1))
    client = _client(monkeypatch, store)

    body = client.post(f"/api/projects/{_UUID}/context-notes?endpoint={ENDPOINT}").json()
    assert body["run_id"] == "run-1"
    assert started == [(_UUID, ENDPOINT)]

    status = client.get(f"/api/projects/{_UUID}/context-notes/status/run-1").json()
    assert (status["status"], status["endpoint"], status["objects_total"]) == ("running", ENDPOINT, 1)


def test_an_unknown_run_id_404s(tmp_path, monkeypatch):
    store = LocalFileStore(str(tmp_path))
    store.save(_project(report=_report(), plan=_translated_plan()))
    monkeypatch.setattr(context_routes.runs, "get_run", lambda run_id: None)
    client = _client(monkeypatch, store)

    assert client.get(f"/api/projects/{_UUID}/context-notes/status/nope").status_code == 404


def test_a_failed_generation_marks_the_run_failed(monkeypatch):
    """generate_ai_notes is fail-soft, so an unusable answer must still read as a
    failed run rather than a successful one with nothing in it."""
    from backend.context_bundle import runs as runs_module

    monkeypatch.setattr(runs_module, "generate_ai_notes",
                        lambda project, endpoint: AiNotes(endpoint=ENDPOINT, success=False,
                                                          error="endpoint unavailable"))
    project = _project(report=_report(), plan=_translated_plan())
    run_id = runs_module.start_run(project, ENDPOINT)
    for _ in range(200):
        state = runs_module.get_run(run_id)
        if state and state.status != "running":
            break
        time.sleep(0.01)

    state = runs_module.get_run(run_id)
    assert state is not None
    assert state.status == "failed"
    assert state.error == "endpoint unavailable"
    assert runs_module.latest_notes(project.id) is None
