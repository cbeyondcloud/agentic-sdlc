"""Tests for the library pipelines `feature-delivery` and `bugfix` (M1-61): each validates,
passes preflight with no problem at all against the standalone policy, has a clean
compatibility check, and runs end to end through the engine on fake models (a dry run:
no model is called, nothing is published)."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

import sdlc_commons.ledger as ledger_module
from agentic.agents.models import GatewaySettings
from agentic.definitions.pipeline import AgentStep, GateStep, LoopStep
from agentic.engine.engine import Engine
from agentic.engine.factory import PydanticStepRunners
from agentic.engine.gates import Approve, Edit, GateRefused, StandaloneAuthorizer
from agentic.engine.preflight import preflight
from agentic.engine.state import RunStore
from agentic.handoffs import check_compatibility, parse_handoff
from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.policy import ResolveContext
from agentic.yamlio import dump_yaml, load_yaml, split_front_matter
from sdlc_commons.gates import ZyrofloGates, regime_block_on, self_approval_refusal
from sdlc_commons.ledger import RunIdentity, RunLedger

PIPELINES = ["bugfix", "feature-delivery"]
ACTIONS = {"github.open_pr"}
#: the build loop's steps when a blocking review loops once: QA tests every round's change
LOOPED = ["develop", "review", "qa", "develop", "review", "qa"]
#: the steps each run takes, with the gates it stops at
EXPECTED = {
    "feature-delivery": (
        ["requirements", "baseline", "design", *LOOPED, "publish"],
        ["requirements-approval", "design-approval", "review-approval"],
    ),
    "bugfix": (
        ["intake", "baseline", "fix-plan", *LOOPED, "publish"],
        ["intake-approval", "review-approval"],
    ),
}
LOOP_ID = {"feature-delivery": "build-review", "bugfix": "fix-review"}


def answer(kind: str, **fields) -> str:
    """A valid answer for `kind`: its template with some fields replaced."""
    header, body = split_front_matter(render_template(BUILTIN_CATALOG.schema(kind)))
    values, _ = load_yaml(header)
    values.update(fields)
    return f"---\n{dump_yaml(values)}---\n{body}\n"


BLOCKING = [{"severity": "high", "issue": "The export has no timeout.", "status": "open"}]
SCRIPT = {  # agent -> its answers, in order (the last one repeats)
    "business-analyst": [answer("requirements")],
    "qa-baseline": [answer("test-report", summary="Baseline: 42 passed, 0 failed.")],
    "design": [answer("design")],
    "developer": [answer("dev-report")],
    "code-review": [answer("review", findings=BLOCKING), answer("review", findings=[])],
    "qa": [answer("test-report")],
}
QA_FAILS = [
    answer(
        "test-report",
        verdict="fail",
        tests={"passed": 41, "failed": 1, "skipped": 0},
        failures=["tests/test_export.py::test_csv"],
    )
]


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.mark.parametrize("name", PIPELINES)
def test_pipeline_passes_preflight_with_no_problems(library, standalone_policy, name):
    report = preflight(
        library.pipeline(name),
        library,
        standalone_policy,
        ResolveContext(),
        actions=ACTIONS,
        available_backends={"native"},
    )
    assert report.problems == [], report.text()  # no errors and no warnings


@pytest.mark.parametrize("name", PIPELINES)
def test_compatibility_check_is_clean(library, standalone_policy, name):
    report = preflight(
        library.pipeline(name), library, standalone_policy, ResolveContext(), actions=ACTIONS
    )
    assert check_compatibility(library.pipeline(name), report.resolved) == []


@pytest.mark.parametrize("name", PIPELINES)
def test_baseline_runs_before_any_code_changes(library, name):
    steps = library.pipeline(name).steps
    agents = [getattr(step, "agent", None) for step in steps]
    first_loop = next(i for i, step in enumerate(steps) if isinstance(step, LoopStep))
    assert agents.index("qa-baseline") < first_loop  # code changes only inside the loop


@pytest.mark.parametrize("name", PIPELINES)
def test_qa_runs_inside_the_build_loop(library, name):
    """DF-238: a QA `fail` routes the work back to the Developer, because QA is the last
    step of the build loop and the loop ends only on a clean review AND a QA pass. A
    person reads the change with its QA report at the gate after the loop. QA runs in every
    round (review F1): the report the loop ends on is always about the current change."""
    steps = library.pipeline(name).steps
    index, loop = next((i, s.loop) for i, s in enumerate(steps) if isinstance(s, LoopStep))
    body = [(s.id, s.agent, s.when) for s in loop.steps]
    assert body == [
        ("develop", "developer", None),
        ("review", "code-review", None),
        ("qa", "qa", None),
    ]
    assert loop.until == "review.open_blocking == 0 && test_report.verdict == 'pass'"
    assert (loop.id, loop.on_exhausted) == (LOOP_ID[name], "gate")
    gate = steps[index + 1]
    assert isinstance(gate, GateStep) and gate.gate == "review-approval"


@pytest.mark.parametrize("name", PIPELINES)
def test_a_person_answers_the_analysts_questions_before_anything_is_built(library, name):
    """Review F1: the Business Analyst's Open Questions go to a person. A gate follows it
    directly, and a rejection sends the work back to it (with the person's answers)."""
    steps = library.pipeline(name).steps
    analyst = next(
        i for i, s in enumerate(steps) if isinstance(s, AgentStep) and s.agent == "business-analyst"
    )
    gate = steps[analyst + 1]
    assert isinstance(gate, GateStep), gate
    assert gate.on_reject in (None, steps[analyst].id)  # None: the step before the gate


def dry_run(library, standalone_policy, name: str, tmp_path: Path, script: dict) -> dict:
    """Run `name` through the engine on scripted fake models, approving every gate it
    stops at. Returns the final state, the PR action's calls, the ledger and the
    instructions each agent ran on."""
    repo = tmp_path / "repo"
    repo.mkdir()
    turns: dict[str, int] = {}
    seen: dict[str, str] = {}

    def model_for(step):
        agent = step.agent.definition.name

        def respond(messages, info):
            seen[agent] = str(getattr(messages[-1], "instructions", "") or "")
            index = turns.get(agent, 0)
            turns[agent] = index + 1
            answers = script[agent]
            return ModelResponse(parts=[TextPart(answers[min(index, len(answers) - 1)])])

        return FunctionModel(respond)

    ledgers: dict[str, RunLedger] = {}

    def recorder_for(run_id: str) -> RunLedger:
        if run_id not in ledgers:
            ledger = RunLedger(repo, run_id)
            ledger.open(
                identity=RunIdentity(team="standalone", policy_version=standalone_policy.version),
                kind="feature" if name == "feature-delivery" else "defect",
                project="demo",
                owner="me",
                criticality="low",
            )
            ledgers[run_id] = ledger
        return ledgers[run_id]

    published: list[str] = []
    engine = Engine(
        definitions=library,
        policy=standalone_policy,
        context=ResolveContext(),
        store=RunStore(repo / ".agentic" / "local" / "runs"),
        recorder_for=recorder_for,
        gates_for=lambda run_id: ZyrofloGates(recorder_for(run_id)),
        runner_factory=PydanticStepRunners(
            identity_for=lambda step: None,
            gateway=GatewaySettings(base_url="https://gateway.example", api_key="k"),
            prices=lambda model: None,
            fallback_estimate_usd=Decimal("0.01"),
            tools_for=lambda step: None,
            state_dir_for=lambda step: tmp_path / "agents" / f"{step.step}-{step.iteration}",
            model_for=model_for,
            policy_version=standalone_policy.version,
        ),
        block_on_for=regime_block_on,
        clock=Clock(),
        actions={"github.open_pr": lambda state: published.append(state.run_id) or "dry run"},
        run_budget_usd=Decimal("50"),
    )
    run_id = f"r-{name}"
    gates: list[str] = []
    refused: str | None = None

    state = asyncio.run(engine.start(name, {"request": "Add CSV export"}, "me@example.com", run_id))
    while state.status == "awaiting_gate":
        gates.append(state.pending.id)
        try:
            state = asyncio.run(
                engine.decide(
                    run_id,
                    Approve(state.pending.id, "me@example.com"),
                    authorizer=StandaloneAuthorizer(),
                    self_approval=self_approval_refusal,
                )
            )
        except GateRefused as error:  # the run keeps waiting where it was
            refused = str(error)
            break
    return {
        "state": state,
        "gates": gates,
        "refused": refused,
        "published": published,
        "ledger": ledgers[run_id],
        "seen": seen,
        "engine": engine,
    }


def walked(state) -> list[str]:
    """The steps the run took, a skipped one marked `:skipped`."""
    return [r.step + (":skipped" if r.outcome == "skipped" else "") for r in state.history]


@pytest.mark.parametrize("name", PIPELINES)
def test_dry_run_completes_through_the_engine(library, standalone_policy, name, tmp_path: Path):
    run = dry_run(library, standalone_policy, name, tmp_path, SCRIPT)
    state, ledger = run["state"], run["ledger"]
    steps, gates = EXPECTED[name]

    assert run["gates"] == gates
    assert state.status == "completed"
    assert walked(state) == steps
    assert run["published"] == [state.run_id]  # QA passed, so the PR action ran
    assert ledger.verify() == []
    for kind, rel in state.latest.items():
        handoff = parse_handoff(
            (ledger.run_dir / rel).read_text(encoding="utf-8"),
            library.handoffs,
            block_on=regime_block_on(state.regime),
        )
        assert handoff.envelope.kind == kind
    for agent, instructions in run["seen"].items():  # each ran on its own library instructions
        assert library.agent(agent).instructions.splitlines()[0] in instructions, agent


@pytest.mark.parametrize("name", PIPELINES)
def test_a_failed_qa_routes_back_to_the_developer(library, standalone_policy, name, tmp_path: Path):
    """DF-238: a QA `fail` sends the work back to the Developer, with the QA report naming
    the failure among its inputs; once QA passes the run completes and publishes."""
    script = {
        **SCRIPT,
        "code-review": [answer("review", findings=[])],
        "qa": [QA_FAILS[0], answer("test-report")],
    }
    run = dry_run(library, standalone_policy, name, tmp_path, script)
    state = run["state"]

    assert state.status == "completed", state.pending
    loop = walked(state)[3:]
    assert loop == ["develop", "review", "qa", "develop", "review", "qa", "publish"]
    assert "tests/test_export.py::test_csv" in run["seen"]["developer"]  # the failing report
    assert run["published"] == [state.run_id]
    assert run["ledger"].verify() == []


@pytest.mark.parametrize("name", PIPELINES)
def test_qa_that_keeps_failing_ends_at_a_gate_never_completed(
    library, standalone_policy, name, tmp_path: Path
):
    """DF-238: QA failing on every pass ends the loop at its gate after max_iterations
    (exit 4); approving it as it stands is refused, nothing is published, and the run is
    never `completed` with a failing QA report."""
    script = {**SCRIPT, "code-review": [answer("review", findings=[])], "qa": QA_FAILS}
    run = dry_run(library, standalone_policy, name, tmp_path, script)
    state = run["state"]

    assert (state.status, state.pending.id) == ("awaiting_gate", f"{LOOP_ID[name]}-exhausted")
    assert run["refused"] and "test_report.verdict == 'pass'" in run["refused"]
    assert [r.step for r in state.history].count("qa") == 3
    assert run["published"] == []
    assert run["ledger"].verify() == []


def accept_the_review(run, name: str):
    """At the gate of a loop exhausted on a review that always blocks, a person accepts
    the review's findings by editing it. Returns the state after the decision."""
    state = run["state"]
    assert state.pending.id == f"{LOOP_ID[name]}-exhausted", state.pending
    path = state.latest["review"]
    text = (run["ledger"].run_dir / path).read_text(encoding="utf-8")
    accepted = text.replace("status: open", "status: accepted").replace(
        "open_blocking: 1", "open_blocking: 0"
    )
    assert accepted != text
    return asyncio.run(
        run["engine"].decide(
            state.run_id,
            Edit(state.pending.id, "lead@example.com", path, accepted),
            authorizer=StandaloneAuthorizer(),
            self_approval=self_approval_refusal,
        )
    )


@pytest.mark.parametrize("name", PIPELINES)
def test_accepting_the_review_never_ends_the_loop_on_a_stale_test_report(
    library, standalone_policy, name, tmp_path: Path
):
    """Review F1: QA tests every round's change, so when a person accepts the review's
    findings at the loop's gate, the loop's `until` reads this round's QA report - never
    the intake baseline's `pass` - and a failing QA still holds the loop."""
    script = {**SCRIPT, "code-review": [answer("review", findings=BLOCKING)], "qa": QA_FAILS}
    run = dry_run(library, standalone_policy, name, tmp_path, script)
    steps = [r.step for r in run["state"].history]
    assert steps.count("qa") == steps.count("review") >= 2  # one QA report per round

    with pytest.raises(GateRefused, match="test_report.verdict == 'pass'"):
        accept_the_review(run, name)
    assert run["published"] == []


@pytest.mark.parametrize("name", PIPELINES)
def test_accepting_the_review_ends_the_loop_on_this_rounds_qa_report(
    library, standalone_policy, name, tmp_path: Path
):
    script = {**SCRIPT, "code-review": [answer("review", findings=BLOCKING)]}
    run = dry_run(library, standalone_policy, name, tmp_path, script)

    state = accept_the_review(run, name)
    assert (state.status, state.pending.id) == ("awaiting_gate", "review-approval")
    report = parse_handoff(
        (run["ledger"].run_dir / state.latest["test-report"]).read_text(encoding="utf-8"),
        library.handoffs,
    )
    assert report.envelope.step == "qa"  # QA's report on the last round, not the baseline
