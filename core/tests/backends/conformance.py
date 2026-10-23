"""Backend conformance: what every backend's runner must do, whatever drives the model
(M1-28, M2-41; spec §9.1, §20). The cases are grouped in named tiers:

  * `output`      - the output contract: a valid answer completes, an invalid one gets one
                    retry, two end invalid, no contract means the text is the output, and
                    an empty answer is never a completed step (DF-433);
  * `limits`      - the step's budget slice and time limit reach the engine;
  * `containment` - the agent process is adopted into the step's process scope and is
                    gone when the scope closes (DF-472); it applies to backends whose
                    capability entry says they start one (`own_process`).

Which tiers apply to a backend is read from the capability matrix
(`agentic.backends.capabilities.CAPABILITIES`), never from a list here.

A backend's harness supplies

    make_runner(agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0,
                over_budget=False) -> AgentRunner

where `answers` are the model's replies in order, `delay_s` slows each reply, and
`over_budget` makes the backend's spend exceed the step's budget slice. Each case is a
function taking (make_runner, agent). `test_conformance.py` runs every tier on every
backend's scripted stand-in; the pinned-binary contract modules run `CASES` too.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import pytest
from _process_support import gone

from agentic.agents.runner import AgentTask
from agentic.backends.capabilities import BackendCapabilities
from agentic.backends.processes import process_scope
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


# ── output ───────────────────────────────────────────────────────────────────


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


def an_empty_answer_is_not_a_completed_step(make_runner, agent):  # DF-433
    """Silence never completes a step: the runner ends it `invalid` (the agent-CLI
    backends) or fails it loudly (native, DF-36: an empty response is the step's failure)."""
    runner = make_runner(agent, answers=[""], budget=slice_(), contract=None)
    try:
        outcome = run(runner)
    except Exception:  # noqa: BLE001 - a failed step is not a completed one
        return
    said = f"an agent that said nothing completed its step (output {outcome.output!r})"
    assert outcome.status != "completed", said


# ── limits ───────────────────────────────────────────────────────────────────


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


# ── containment ──────────────────────────────────────────────────────────────


def the_agent_process_is_contained_in_the_step_scope(make_runner, agent):  # DF-472
    """The runner adopts its agent process into the step's process scope (so a stopped
    run, a time limit or Ctrl-C ends it whole, M2-22), and the scope's close ends it."""
    adopted = []
    with process_scope() as scope:
        add = scope.add

        def spy(tree) -> None:
            adopted.append(tree.pid)
            add(tree)

        scope.add = spy
        outcome = run(make_runner(agent, answers=["VALID"], budget=slice_(), contract=None))
        assert outcome.status == "completed", outcome.detail
    assert adopted, "the runner never adopted its agent process into the step's scope"
    assert all(gone(pid) for pid in adopted if pid), "an adopted process outlived the scope"


@dataclass(frozen=True)
class Tier:
    name: str
    summary: str
    cases: tuple[Callable, ...]
    applies: Callable[[BackendCapabilities], bool] = lambda caps: True


TIERS = (
    Tier(
        "output",
        "the output contract: completion, one retry, invalid, raw text, no empty answer",
        (
            a_valid_answer_completes,
            an_invalid_answer_gets_one_retry,
            two_invalid_answers_end_invalid,
            without_a_contract_the_text_is_the_output,
            an_empty_answer_is_not_a_completed_step,
        ),
    ),
    Tier(
        "limits",
        "the step's budget slice and time limit reach the engine",
        (spending_past_the_step_slice_reaches_the_engine, the_time_limit_stops_the_run),
    ),
    Tier(
        "containment",
        "the agent process lives and dies inside the step's process scope",
        (the_agent_process_is_contained_in_the_step_scope,),
        applies=lambda caps: caps.own_process,
    ),
)
#: Every case, for the pinned-binary contract modules (which run the same checks).
CASES = [case for tier in TIERS for case in tier.cases]


# ── the report ───────────────────────────────────────────────────────────────

PASS, FAIL, NOT_APPLICABLE, SKIPPED = "pass", "FAIL", "n/a", "skipped"


def verdicts(results: list[tuple[str, str, str]]) -> dict[str, dict[str, str]]:
    """`{backend: {tier: verdict}}` from `(backend, tier, outcome)` rows, one per case: a
    tier fails when any case failed, passes when every case that ran passed, and is
    skipped when none ran; `n/a` rows (the matrix says the tier does not apply) stay n/a."""
    found: dict[str, dict[str, list[str]]] = {}
    for backend, tier, outcome in results:
        found.setdefault(backend, {}).setdefault(tier, []).append(outcome)
    table: dict[str, dict[str, str]] = {}
    for backend, tiers in found.items():
        for tier, outcomes in tiers.items():
            if FAIL in outcomes:
                verdict = FAIL
            elif PASS in outcomes:
                verdict = PASS
            elif outcomes and all(o == NOT_APPLICABLE for o in outcomes):
                verdict = NOT_APPLICABLE
            else:
                verdict = SKIPPED
            table.setdefault(backend, {})[tier] = verdict
    return table


def summary_lines(table: dict[str, dict[str, str]], evidence: str, meaning: str) -> list[str]:
    """The conformance table for the terminal: one row per backend, one column per tier."""
    names = [tier.name for tier in TIERS]
    width = max([len("backend"), *map(len, table)])
    lines = [
        f"runner conformance - evidence tier: {evidence} ({meaning})",
        "  " + "backend".ljust(width) + "".join(f"  {name:<12}" for name in names),
    ]
    for backend in sorted(table):
        row = table[backend]
        lines.append(
            "  " + backend.ljust(width) + "".join(f"  {row.get(n, '-'):<12}" for n in names)
        )
    failing = [f"{b}/{t}" for b in sorted(table) for t, v in table[b].items() if v == FAIL]
    if failing:
        lines.append("  FAILING TIERS: " + ", ".join(failing))
    return lines
