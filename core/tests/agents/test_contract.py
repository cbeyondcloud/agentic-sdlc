"""Tests for the handoff validation retry: errors fed back once, then invalid (M1-20, spec §7.2)."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from _resume_helper import write_tool
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentic.agents.runner import (
    OUTPUT_RETRIES,
    AgentOutcome,
    AgentTask,
    PydanticAIRunner,
    ResumeError,
    retry_message,
)
from agentic.handoffs.contract import HandoffContract
from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.transport import BudgetLedger

FIXED = datetime(2026, 10, 19, 10, 42, tzinfo=UTC)
DESIGN = BUILTIN_CATALOG.schema("design")
CONTRACT = HandoffContract(DESIGN, "r-sp5-1", "design", "design@1.4.0", clock=lambda: FIXED)
VALID = render_template(DESIGN)  # the template is itself a valid answer
PROSE = "I think we should add a queue."
TASK = AgentTask(instructions="You are the Design agent.", inputs={}, prompt="Design it.")


def runner(resolved, model, tmp_path: Path, **kwargs) -> PydanticAIRunner:
    step = BudgetLedger(Decimal("5")).run.open("design", "step", Decimal("1"))
    return PydanticAIRunner(resolved, model, budget=step, state_dir=tmp_path, **kwargs)


def run(r: PydanticAIRunner) -> AgentOutcome:
    return asyncio.run(r.run(TASK))


def answers(*texts: str, seen: list | None = None) -> FunctionModel:
    """Answers with each text in turn (the last one repeats), recording what it was sent."""
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(list(messages))
        text = texts[min(calls["n"], len(texts) - 1)]
        calls["n"] += 1
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(respond)


def test_a_valid_answer_completes_with_the_final_document(resolved, tmp_path):
    outcome = run(runner(resolved, answers(VALID), tmp_path, contract=CONTRACT))

    assert outcome.status == "completed"
    assert outcome.output.startswith("---\nkind: design\nschema_version: 1\nrun_id: r-sp5-1\n")


def test_an_invalid_answer_is_retried_once_with_its_errors(resolved, tmp_path):
    seen: list = []
    outcome = run(runner(resolved, answers(PROSE, VALID, seen=seen), tmp_path, contract=CONTRACT))

    assert outcome.status == "completed" and len(seen) == 2
    feedback = seen[1][-1].parts[-1].model_response()
    assert "Your handoff document is not valid:" in feedback
    assert "must be one handoff document" in feedback


def test_two_invalid_answers_end_invalid_and_keep_the_text(resolved, tmp_path):  # Review Focus 5
    seen: list = []
    r = runner(resolved, answers(PROSE, PROSE, PROSE, seen=seen), tmp_path, contract=CONTRACT)

    outcome = run(r)
    assert (outcome.status, outcome.output, outcome.stopped) == ("invalid", PROSE, None)
    assert outcome.errors and "must be one handoff document" in outcome.errors[0]
    assert len(seen) == 1 + OUTPUT_RETRIES  # never a third try


def test_the_model_is_told_the_output_format(resolved, tmp_path):
    seen: list = []
    run(runner(resolved, answers(VALID, seen=seen), tmp_path, contract=CONTRACT))

    instructions = seen[0][0].instructions
    assert '<output-format kind="design">' in instructions and "## Summary" in instructions
    assert instructions.index("You are the Design agent.") < instructions.index("<output-format")


def test_a_pause_for_approval_is_not_validated(resolved, tmp_path):
    calls = FunctionModel(
        lambda m, i: ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt"})])
    )
    r = runner(resolved, calls, tmp_path, contract=CONTRACT, tools=[write_tool(tmp_path)])

    outcome = run(r)
    assert outcome.status == "paused" and outcome.state_digest


def test_without_a_contract_any_text_completes(resolved, tmp_path):
    outcome = run(runner(resolved, answers(PROSE), tmp_path))

    assert (outcome.status, outcome.output) == ("completed", PROSE)


def test_the_retry_message_lists_each_error():
    text = retry_message(["kind: wrong", "body: empty"])

    assert text == (
        "Your handoff document is not valid:\n- kind: wrong\n- body: empty\n"
        "Answer again with the whole document, corrected: a line '---', the YAML fields, "
        "a line '---', then the Markdown body."
    )


def test_the_contract_instructions_wrap_the_template():
    text = CONTRACT.instructions

    assert text.startswith('<output-format kind="design">\n')
    assert text.endswith("</output-format>") and render_template(DESIGN) in text


# ── final review fixes (I3, I4, M1) ───────────────────────────────────────────


def test_an_unreadable_answer_is_fed_back_not_raised(resolved, tmp_path, monkeypatch):  # I3
    import agentic.handoffs.contract as contract_module

    def explode(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(contract_module, "finalise_handoff", explode)
    outcome = run(runner(resolved, answers(PROSE), tmp_path, contract=CONTRACT))

    assert outcome.status == "invalid" and "could not be read" in outcome.errors[0]


def test_resume_needs_the_contract_the_run_paused_with(resolved, tmp_path):  # review I4
    calls = FunctionModel(
        lambda m, i: ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt"})])
    )
    tools = [write_tool(tmp_path)]
    paused = run(runner(resolved, calls, tmp_path, contract=CONTRACT, tools=tools))
    decisions = {paused.pending[0]: True}

    without = runner(resolved, answers(PROSE), tmp_path, tools=tools)
    with pytest.raises(ResumeError, match="contract"):
        asyncio.run(without.resume(paused.state_path, decisions, paused.state_digest))
    state = json.loads(paused.state_path.read_text(encoding="utf-8"))
    assert state["contract"] == CONTRACT.digest


def test_a_contract_checks_block_on_and_clock_at_once():  # review M1
    with pytest.raises(ValueError, match="floor"):
        HandoffContract(DESIGN, "r", "s", "a@1", block_on=frozenset(), clock=lambda: FIXED)
    with pytest.raises(ValueError, match="timezone"):
        HandoffContract(DESIGN, "r", "s", "a@1", clock=lambda: datetime(2026, 10, 19))
