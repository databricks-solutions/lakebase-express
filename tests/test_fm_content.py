"""chat_text — normalizing chat responses from reasoning models.

Plain chat models return ``message.content`` as a string, but reasoning models
(e.g. databricks-claude-fable-5, thinking always on) return a list of content
blocks: reasoning block(s) first, then text block(s) with the final answer.
The old ``content.strip()`` parsing crashed on the list with
"'list' object has no attribute 'strip'". chat_text must keep only the final
answer text and drop reasoning entirely.
"""
from types import SimpleNamespace

from backend.fm_params import chat_text


def _resp(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_plain_string_passes_through():
    assert chat_text(_resp('{"ok": true}')) == '{"ok": true}'


def test_reasoning_blocks_are_dropped():
    # Shape observed live from databricks-claude-fable-5.
    content = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "…", "signature": "x"}]},
        {"type": "text", "text": '{"ok": true}'},
    ]
    assert chat_text(_resp(content)) == '{"ok": true}'


def test_multiple_text_parts_join_in_order():
    content = [
        {"type": "reasoning", "summary": []},
        {"type": "text", "text": "part one, "},
        {"type": "reasoning", "summary": []},
        {"type": "text", "text": "part two"},
    ]
    assert chat_text(_resp(content)) == "part one, part two"


def test_reasoning_only_response_yields_empty_string():
    content = [{"type": "reasoning", "summary": []}]
    assert chat_text(_resp(content)) == ""


def test_malformed_parts_are_skipped():
    content = ["not-a-dict", {"type": "text"}, {"type": "text", "text": None}, {"type": "text", "text": "ok"}]
    assert chat_text(_resp(content)) == "ok"


def test_none_content_yields_empty_string():
    assert chat_text(_resp(None)) == ""


def test_unexpected_shape_stringifies():
    assert chat_text(_resp({"odd": "shape"})) == "{'odd': 'shape'}"
