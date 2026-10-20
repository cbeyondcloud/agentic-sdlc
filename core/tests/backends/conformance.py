"""Backend conformance: what every backend's runner must do, whatever drives the model
(M1-28; spec §9.1 "each backend has contract tests"). A backend's test module supplies

    make_runner(agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0,
                over_budget=False) -> AgentRunner

where `answers` are the model's replies in order, `delay_s` slows each reply, and
`over_budget` makes the backend's spend exceed the step's budget slice. Each case is
a function taking (make_runner, agent).
"""

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import pytest

from agentic.agents.runner import AgentTask
from agentic.transport import BudgetLedger, LimitHit

TASK = AgentTask(instructions="You are the Developer.", inputs={}, prompt="Write the report.")


@dataclass(frozen=True)
class FakeContract:
    """An output contract that accepts any answer starting with 'VALID'."""

    instructions: str = "Answer with a line starting VALID."
    digest: str = "sha256:" + "0" * 64

    def check(self, text: str) -> tuple[str | None, list[str]]:
        return (text, []) if text.startswith("VALID") else (None, ["it must start with VALID"])


def slice_(limit: str = "5"):
    return BudgetLedger(Decimal("10")).run.open("develop-1", "step", Decimal(limit))


def run(runner):
    return asyncio.run(runner.run(TASK))


def a_valid_answer_completes(make_runner, agent):
    outcome = run(
        make_runner(agent, answers=["VALID report"], budget=slice_(), contract=FakeContract())
    )
    assert (outcome.status, outcome.output) == ("completed", "VALID report")
    assert outcome.usage is not None


def an_invalid_answer_gets_one_retry(make_runner, agent):
    outcome = run(
        make_runner(
            agent, answers=["prose", "VALID second try"], budget=slice_(), contract=FakeContract()
        )
    )
    assert (outcome.status, outcome.output) == ("completed", "VALID second try")


def two_invalid_answers_end_invalid(make_runner, agent):
    outcome = run(
        make_runner(
            agent, answers=["prose", "more prose"], budget=slice_(), contract=FakeContract()
        )
    )
    assert outcome.status == "invalid"
    assert outcome.output == "more prose" and outcome.errors


def without_a_contract_the_text_is_the_output(make_runner, agent):
    outcome = run(make_runner(agent, answers=["any text"], budget=slice_(), contract=None))
    assert (outcome.status, outcome.output) == ("completed", "any text")


def spending_past_the_step_slice_reaches_the_engine(make_runner, agent):
    runner = make_runner(
        agent, answers=["VALID"], budget=slice_("0.01"), contract=None, over_budget=True
    )
    with pytest.raises(LimitHit):
        run(runner)


def the_time_limit_stops_the_run(make_runner, agent):
    runner = make_runner(
        agent, answers=["VALID late"], budget=slice_(), contract=None, time_limit_s=1, delay_s=5
    )
    outcome = run(runner)
    assert (outcome.status, outcome.stopped) == ("stopped", "time")


CASES = [
    a_valid_answer_completes,
    an_invalid_answer_gets_one_retry,
    two_invalid_answers_end_invalid,
    without_a_contract_the_text_is_the_output,
    spending_past_the_step_slice_reaches_the_engine,
    the_time_limit_stops_the_run,
]
