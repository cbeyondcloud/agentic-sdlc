"""Each agent step is a span carrying the agent's gen_ai identity; Pydantic AI's agent-run
and model-call spans (pinned gen_ai attributes) nest under it (M1-43). Content capture
is off: no prompt or answer text is exported."""

import asyncio

from _engine_support import answer

from agentic.telemetry.semconv import span_problems

PIPELINE = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
"""
SECRET_PROMPT = "export the customer ledger as CSV"


def run(make_harness):
    harness = make_harness(PIPELINE, {"designer": [answer("design", summary="the-answer-text")]})
    state = asyncio.run(harness.engine.start("test", {"request": SECRET_PROMPT}, "dev@x", "r-sp"))
    assert state.status == "completed"
    return harness


def named(spans, prefix):
    return [s for s in spans.get_finished_spans() if s.name.startswith(prefix)]


def test_step_agent_run_and_model_call_spans_match_the_pinned_schema(make_harness, spans):
    run(make_harness)
    (step,) = named(spans, "step design")
    (agent_run,) = named(spans, "invoke_agent ")
    calls = named(spans, "chat ")
    assert calls, [s.name for s in spans.get_finished_spans()]

    assert span_problems("step", step.attributes) == []
    assert step.attributes["agentic.run_id"] == "r-sp"
    assert step.attributes["agentic.outcome"] == "completed"
    assert step.attributes["gen_ai.agent.name"] == "designer"
    assert step.instrumentation_scope.schema_url.endswith("1.37.0")
    assert span_problems("agent_run", agent_run.attributes) == []
    assert agent_run.attributes["gen_ai.agent.name"] == "designer"
    for call in calls:
        assert span_problems("model_call", call.attributes) == []


def test_model_call_spans_are_under_the_step_span(make_harness, spans):
    run(make_harness)
    (step,) = named(spans, "step design")
    ids = {s.context.span_id: s for s in spans.get_finished_spans()}
    for call in named(spans, "chat "):
        parent, chain = call.parent, []
        while parent is not None and parent.span_id in ids:
            chain.append(parent.span_id)
            parent = ids[parent.span_id].parent
        assert step.context.span_id in chain
        assert call.context.trace_id == step.context.trace_id


def test_no_prompt_or_answer_text_reaches_a_span(make_harness, spans):  # review focus 4
    run(make_harness)
    exported = repr([dict(s.attributes) for s in spans.get_finished_spans()])
    assert SECRET_PROMPT not in exported
    assert "the-answer-text" not in exported
    assert "You are designer" not in exported  # the agent's instructions
