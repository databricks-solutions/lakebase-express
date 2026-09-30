"""The Jinja prompt templates and the loader they share.

These guard a failure mode that is otherwise silent: a template that does not reach
a deployed build, or a Jinja tag left unrendered, degrades a Foundation Model call
rather than raising. Content assertions live with each module's own tests; this
file checks that every prompt loads at all, and that the list below stays complete.
"""
import re
from pathlib import Path

import pytest

from backend import prompts
from backend.assessment import ai_analysis
from backend.context_bundle import ai_notes
from backend.query_parity import generator
from backend.schema_migration import ai_translator
from backend.validation import agent, fixer

BACKEND = Path(__file__).resolve().parent.parent / "backend"

# Every module that sends a system prompt. Kept explicit rather than discovered by
# importing the whole package — test_every_prompt_module_is_listed_here fails if one
# is added without a line here.
PROMPT_MODULES = [
    ai_analysis,
    ai_notes,
    ai_translator,
    generator,
    fixer,
    agent,
]


@pytest.mark.parametrize("module", PROMPT_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_every_system_prompt_renders(module):
    prompt = module._system_prompt()
    assert len(prompt) > 500, "a prompt this short means the template barely loaded"
    # Unrendered Jinja, or a comment header that leaked into what the model sees.
    for tag in ("{#", "#}", "{{", "}}", "{%", "%}"):
        assert tag not in prompt, f"{tag} survived rendering"
    assert prompt == prompt.strip()


def test_every_prompt_module_is_listed_here():
    """A new prompt module must be added to PROMPT_MODULES, not just written."""
    defined = {
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if "__pycache__" not in path.parts
        and re.search(r"^def _system_prompt\(", path.read_text(), re.MULTILINE)
    }
    listed = {
        Path(m.__file__).resolve().relative_to(BACKEND).as_posix() for m in PROMPT_MODULES
    }
    assert defined == listed


def test_no_module_still_holds_a_hardcoded_system_prompt():
    """The point of the templates: prompt prose is tunable without editing Python."""
    offenders = [
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if "__pycache__" not in path.parts and "_SYSTEM_PROMPT" in path.read_text()
    ]
    assert offenders == []


# --- The loader -------------------------------------------------------------------


def test_missing_template_raises_rather_than_returning_empty():
    """A template that did not ship must fail loudly, not silently weaken a call."""
    from jinja2 import TemplateNotFound

    with pytest.raises(TemplateNotFound):
        prompts.render(ai_translator.__file__, "no_such_prompt.system.jinja")


def test_environment_is_cached_per_directory():
    a = prompts._env(str(BACKEND / "validation" / "prompts"))
    b = prompts._env(str(BACKEND / "validation" / "prompts"))
    c = prompts._env(str(BACKEND / "query_parity" / "prompts"))
    assert a is b and a is not c


def test_variables_render_when_a_template_uses_them(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "t.jinja").write_text("hello {{ name }}\n")
    assert prompts.render(str(tmp_path / "mod.py"), "t.jinja", name="world") == "hello world"
