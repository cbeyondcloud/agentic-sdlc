"""Tests for the pinned span schema and the local span exporter (M1-43)."""

import json
from pathlib import Path

from agentic.telemetry.semconv import (
    AGENT_RUN_SPAN,
    MODEL_CALL_SPAN,
    SCHEMA_URL,
    SEMCONV_VERSION,
    STEP_SPAN,
    span_problems,
)


def valid(schema) -> dict:
    return {name: (1 if kind is int else "x") for name, kind in schema.items()}


def test_the_schema_is_pinned_to_one_semconv_version():
    assert SCHEMA_URL == f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}"
    assert {"gen_ai.agent.name", "gen_ai.request.model", "agentic.run_id"} <= set(STEP_SPAN)
    assert {"gen_ai.operation.name", "gen_ai.agent.name"} <= set(AGENT_RUN_SPAN)
    assert {"gen_ai.request.model", "gen_ai.usage.input_tokens"} <= set(MODEL_CALL_SPAN)


def test_span_problems_names_missing_and_mistyped_attributes():
    good = valid(STEP_SPAN)
    assert span_problems("step", good) == []

    bad = dict(good)
    del bad["gen_ai.agent.name"]
    bad["agentic.iteration"] = "two"
    problems = span_problems("step", bad)
    assert any("gen_ai.agent.name" in p and "missing" in p for p in problems)
    assert any("agentic.iteration" in p and "int" in p for p in problems)


def test_the_operation_name_must_match_the_span_kind():
    attributes = valid(MODEL_CALL_SPAN)
    attributes["gen_ai.operation.name"] = "chat"
    assert span_problems("model_call", attributes) == []
    attributes["gen_ai.operation.name"] = "invoke_agent"
    assert any("gen_ai.operation.name" in p for p in span_problems("model_call", attributes))


def test_jsonl_exporter_writes_one_line_per_span(tmp_path: Path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from agentic.telemetry.sdk import JsonlSpanExporter

    path = tmp_path / "run" / "traces.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(path)))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("outer") as outer:
        outer.set_attribute("agentic.run_id", "r-1")
        with tracer.start_as_current_span("inner"):
            pass
    provider.shutdown()

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["name"] for line in lines] == ["inner", "outer"]
    assert lines[1]["attributes"] == {"agentic.run_id": "r-1"}
    assert lines[0]["parent_span_id"] == lines[1]["span_id"]
    assert lines[0]["trace_id"] == lines[1]["trace_id"] and len(lines[0]["trace_id"]) == 32
