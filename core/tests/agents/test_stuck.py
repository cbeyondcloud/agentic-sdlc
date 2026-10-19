"""Tests for the stuck detector (OpenHands port) and the loop oscillation check (M1-18, §8.5)."""

import asyncio
from decimal import Decimal

from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.agents.stuck import (
    Action,
    Message,
    Observation,
    StuckDetector,
    StuckThresholds,
    UserMessage,
    events_from_messages,
    oscillation,
    stuck_check,
)
from agentic.transport import BudgetLedger

DETECTOR = StuckDetector(StuckThresholds())


def pairs(*steps: tuple[Action, Observation]) -> list:
    return [event for pair in steps for event in pair]


A = Action("read_file", "args:a.py")
B = Action("read_file", "args:b.py")
SAME = Observation("read_file", "content:nothing new")
OTHER = Observation("read_file", "content:other")


def test_repeated_identical_actions_are_stuck():
    verdict = DETECTOR.check(pairs(*[(A, SAME)] * 4))

    assert verdict is not None and verdict.pattern == "repeat"


def test_three_repeats_are_not_yet_stuck():
    assert DETECTOR.check(pairs(*[(A, SAME)] * 3)) is None


def test_repeated_errors_are_stuck():
    # the same call failing with different messages: not a repeat, an error loop
    errors = [Observation("read_file", f"error:{n}", error=True) for n in range(4)]

    verdict = DETECTOR.check(pairs(*[(A, e) for e in errors]))
    assert verdict is not None and verdict.pattern == "error_loop"


def test_three_errors_are_not_yet_stuck():  # one repeat past the threshold, as in OpenHands
    errors = [Observation("read_file", f"error:{n}", error=True) for n in range(3)]

    assert DETECTOR.check(pairs(*[(A, e) for e in errors])) is None


def test_ping_pong_is_stuck():
    verdict = DETECTOR.check(pairs(*[(A, SAME), (B, OTHER)] * 3))

    assert verdict is not None and verdict.pattern == "ping_pong"


def test_monologue_is_stuck():
    verdict = DETECTOR.check(
        [
            Message("text:let me think"),
            Message("text:let me think again"),
            Message("text:still thinking"),
        ]
    )

    assert verdict is not None and verdict.pattern == "monologue"


def test_progress_is_not_stuck():
    steps = [
        (Action("read_file", f"args:{n}.py"), Observation("read_file", f"content:{n}"))
        for n in range(10)
    ]

    assert DETECTOR.check(pairs(*steps)) is None


def test_a_user_message_resets_the_window():
    events = pairs(*[(A, SAME)] * 3) + [UserMessage("text:try b")] + pairs(*[(A, SAME)] * 2)

    assert DETECTOR.check(events) is None


def test_thresholds_can_be_set():
    strict = StuckDetector(StuckThresholds(action_observation=2))

    assert strict.check(pairs(*[(A, SAME)] * 2)).pattern == "repeat"


# ── from Pydantic AI messages ─────────────────────────────────────────────────


def test_events_come_from_pydantic_ai_messages():
    messages = [
        ModelRequest(parts=[UserPromptPart("fix it")]),
        ModelResponse(parts=[TextPart("checking"), ToolCallPart("read_file", {"path": "a.py"})]),
        ModelRequest(parts=[ToolReturnPart("read_file", "x = 1", tool_call_id="1")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "a.py"})]),
        ModelRequest(parts=[RetryPromptPart("bad path", tool_name="read_file", tool_call_id="2")]),
        ModelResponse(parts=[TextPart("done")]),
    ]

    events = events_from_messages(messages)
    assert [type(e).__name__ for e in events] == [
        "UserMessage",
        "Action",
        "Observation",
        "Action",
        "Observation",
        "Message",
    ]
    assert events[1] == events[3]  # same call, ids ignored
    assert events[4].error and not events[2].error


def test_a_runner_with_the_detector_stops_a_looping_agent(resolved, tmp_path):
    def look(path: str) -> str:
        """Look at a file."""
        return "nothing new"

    loop = FunctionModel(lambda m, i: ModelResponse(parts=[ToolCallPart("look", {"path": "a"})]))
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal("1"))
    runner = PydanticAIRunner(
        resolved,
        loop,
        budget=step,
        state_dir=tmp_path,
        tools=[Tool(look)],
        stuck_check=stuck_check(DETECTOR),
    )

    outcome = asyncio.run(runner.run(AgentTask("You are stuck.", {}, "go")))
    assert (outcome.status, outcome.stopped) == ("stopped", "stuck")


# ── the pipeline-level check ──────────────────────────────────────────────────


def test_oscillation_across_loop_iterations():
    findings = ["F1: missing test for retry", "F2: wrong error code"]

    assert oscillation(findings, "  f1:  MISSING test for retry ")  # the same finding again
    assert not oscillation(findings, "F3: unused import")


def test_oscillation_only_looks_back_a_window():
    history = ["A", "B", "C"]

    assert not oscillation(history, "A", window=2)
    assert oscillation(history, "B", window=2)
