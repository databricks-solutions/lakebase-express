"""Loader for the Jinja prompt templates that live beside the code using them.

System prompts are templates in a ``prompts/`` directory rather than string
literals in Python, so they can be read and tuned as prose — by someone who knows
the migration domain but not this codebase — without editing a module or
re-deriving where a triple-quoted string ends. Each package that calls a
Foundation Model keeps its own ``prompts/``; this is the one loader they share,
with a Jinja environment cached per directory.

The convention the callers follow: the *system* prompt is a template, because it
is the same for every call and is mostly prose. The *user* message is assembled in
Python, because it is per-object and mostly logic — schema mappings, trigger
naming, the scratch-collection decisions. Templating that would move branching
into Jinja, which is the opposite of the point.
"""
from __future__ import annotations

import functools
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


@functools.lru_cache(maxsize=None)
def _env(prompt_dir: str) -> Environment:
    return Environment(
        loader=FileSystemLoader(prompt_dir),
        autoescape=False,  # prompts are plain text, not HTML
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render(module_file: str, template: str, **variables: object) -> str:
    """Render ``template`` from the ``prompts/`` directory beside ``module_file``.

    Pass ``__file__`` as ``module_file``: a prompt belongs with the code that sends
    it. Trailing whitespace is stripped, so a template can end with a newline
    without that reaching the model.
    """
    directory = str(Path(module_file).parent / "prompts")
    return _env(directory).get_template(template).render(**variables).strip()
