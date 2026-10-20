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
from agentic.engine.engine import Engine
from agentic.engine.factory import PydanticStepRunners
from agentic.engine.gates import Approve, StandaloneAuthorizer
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
#: the steps each run takes, with the gates it stops at (a blocking review loops once)
EXPECTED = {
    "feature-delivery": (
        ["requirements", "baseline", "design", "develop", "review", "develop", "review", "qa"],
        ["design-approval", "review-approval"],
    ),
    "bugfix": (
        ["intake", "baseline", "fix-plan", "develop", "review", "develop", "review", "qa"],
        ["review-approval"],
    ),
}


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
    agents = [getattr(step, "agent", None) for step in library.pipeline(name).steps]
    assert agents.index("qa-baseline") < agents.index(None)  # before the first gate or loop


@pytest.mark.parametrize("name", PIPELINES)
def test_dry_run_completes_through_the_engine(library, standalone_policy, name, tmp_path: Path):
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
            answers = SCRIPT[agent]
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
    steps, gates = EXPECTED[name]

    state = asyncio.run(engine.start(name, {"request": "Add CSV export"}, "me@example.com", run_id))
    for gate in gates:
        assert (state.status, state.pending.id) == ("awaiting_gate", gate), state.status
        decision = Approve(gate, "me@example.com")
        state = asyncio.run(
            engine.decide(
                run_id,
                decision,
                authorizer=StandaloneAuthorizer(),
                self_approval=self_approval_refusal,
            )
        )

    assert state.status == "completed"
    assert [record.step for record in state.history if record.step != "publish"] == steps
    assert published == [run_id]  # QA passed, so the PR action ran
    ledger = ledgers[run_id]
    assert ledger.verify() == []
    for kind, rel in state.latest.items():
        handoff = parse_handoff(
            (ledger.run_dir / rel).read_text(encoding="utf-8"),
            library.handoffs,
            block_on=regime_block_on(state.regime),
        )
        assert handoff.envelope.kind == kind
    for agent, instructions in seen.items():  # each agent ran on its own library instructions
        assert library.agent(agent).instructions.splitlines()[0] in instructions, agent
