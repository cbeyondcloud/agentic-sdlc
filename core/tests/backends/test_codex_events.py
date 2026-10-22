"""Tests for reading `codex exec --json` events (M2-40, spike S3 Q7/Q8/Q11): the final
agent message is the answer, `turn.completed.usage` is what the step is charged from,
tool items are counted for the turn limit, and a failed turn says why."""

import json

from agentic.backends.codex import EventReader, unwrap_document


def read(*events) -> EventReader:
    reader = EventReader()
    for event in events:
        reader.feed(json.dumps(event) if isinstance(event, dict) else event)
    return reader


def message(text):
    return {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": text}}


def test_final_agent_message_is_the_answer():
    reader = read(message("first"), {"type": "turn.started"}, message("last"))
    assert reader.text == "last"


def test_turn_usage_is_kept():
    usage = {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 40}
    assert read({"type": "turn.completed", "usage": usage}).usage == usage


def test_tool_items_are_counted_once_each():
    item = {"id": "c1", "type": "command_execution", "command": "ls"}
    reader = read(
        {"type": "item.started", "item": item},
        {"type": "item.completed", "item": item},
        {"type": "item.completed", "item": {**item, "id": "c2", "type": "file_change"}},
        {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "hm"}},
    )
    assert reader.tool_calls == 2


def test_turn_failed_and_error_events_say_why():
    assert read({"type": "turn.failed", "error": {"message": "stream closed"}}).error == (
        "stream closed"
    )
    assert read({"type": "error", "message": "bad config"}).error == "bad config"


def test_noise_and_blank_lines_are_ignored():
    reader = read("", "not json", "[1, 2]", message("ok"))
    assert (reader.text, reader.error) == ("ok", None)


def test_schema_envelope_is_unwrapped():
    assert (
        unwrap_document('{"document": "---\\nkind: x\\n---\\nbody"}') == "---\nkind: x\n---\nbody"
    )


def test_anything_else_is_kept_as_written():
    for text in ("plain prose", '{"other": 1}', '{"document": 3}', "[]"):
        assert unwrap_document(text) == text
