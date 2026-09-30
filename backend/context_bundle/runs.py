"""Background run for the model notes, and lookup of the latest finished one.

The notes pass reads every translated object through a Foundation Model: measured
at just over four minutes on a 29-object database, well past the Databricks Apps
~120s front-proxy timeout, which drops the connection mid-request. So it runs on a
daemon thread and the UI polls — the same pattern as plan builds, data loads and
validation.

Because run state is persisted (``backend/run_store.py``), the finished notes are
also how the skill keeps them: ``latest_notes`` finds the newest successful run for
a project, so leaving the module and coming back does not mean paying for the model
again, and nothing has to be written onto the project row.
"""
from __future__ import annotations

import logging
import threading
import uuid

from backend.context_bundle.ai_notes import code_items, generate_ai_notes
from backend.context_bundle.models import AiNotes, AiNotesRunState
from backend.projects.models import Project
from backend.run_registry import RunRegistry

log = logging.getLogger("lakebase_express.context_notes")

_REGISTRY: RunRegistry[AiNotesRunState] = RunRegistry("context_notes", AiNotesRunState)

# How far back to look for a finished run before giving up on reusing one.
_LOOKBACK = 20


def get_run(run_id: str) -> AiNotesRunState | None:
    return _REGISTRY.get(run_id)


def start_run(project: Project, endpoint: str | None = None) -> str:
    run_id = str(uuid.uuid4())
    state = AiNotesRunState(
        run_id=run_id,
        status="running",
        endpoint=endpoint or "",
        objects_total=len(code_items(project)),
    )
    _REGISTRY.create(state, project.id)
    threading.Thread(target=_execute, args=(run_id, project, endpoint), daemon=True).start()
    return run_id


def _execute(run_id: str, project: Project, endpoint: str | None) -> None:
    try:
        notes = generate_ai_notes(project, endpoint)
        _REGISTRY.update(run_id, lambda s: (
            # generate_ai_notes is fail-soft, so an unusable answer is a failed run
            # rather than an exception.
            setattr(s, "status", "success" if notes.success else "failed"),
            setattr(s, "endpoint", notes.endpoint),
            setattr(s, "notes", notes),
            setattr(s, "error", notes.error),
        ))
    except Exception as exc:  # surfaced to the polling UI
        log.exception("Model notes run %s failed", run_id)
        _REGISTRY.update(run_id, lambda s: (
            setattr(s, "status", "failed"), setattr(s, "error", str(exc)),
        ))


def latest_notes(project_id: str) -> AiNotes | None:
    """Notes from the newest successful run for this project, or None."""
    for record in _REGISTRY.list(limit=_LOOKBACK, project_id=project_id):
        if record.status != "success":
            continue
        state = _REGISTRY.get(record.run_id)
        if state and state.notes and state.notes.success:
            notes = state.notes
            # Notes written before they carried a timestamp still have a run row with
            # one, and their age is the whole point — they are replayed on every
            # export, so "when" is what says whether they still describe the plan.
            if not notes.generated_at and record.updated_at:
                notes = notes.model_copy(update={"generated_at": record.updated_at})
            return notes
    return None
