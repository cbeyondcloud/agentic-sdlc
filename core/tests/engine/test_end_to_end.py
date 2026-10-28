"""A whole pipeline run through the real pieces: preflight, the engine, the production
runner factory, the run ledger and Zyroflo's approval ledger (SP6 Task 7)."""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from _engine_support import AGENTS, FakeClock, answer, review, write
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

import sdlc_commons.ledger as ledger_module
from agentic.agents.models import GatewaySettings
from agentic.definitions import DefinitionSet
from agentic.engine.engine import Engine
from agentic.engine.factory import PydanticStepRunners
from agentic.engine.gates import Approve, StandaloneAuthorizer
from agentic.engine.preflight import preflight
from agentic.engine.state import RunStore
from agentic.handoffs import parse_handoff
from agentic.policy import ResolveContext, load_policy
from agentic.transport.budget import BudgetLedger
from agentic.transport.cost import CallCost
from sdlc_commons.gates import ZyrofloGates, regime_block_on, self_approval_refusal
from sdlc_commons.ledger import RunIdentity, RunLedger

FEATURE_DELIVERY = """\
name: feature-delivery
version: 1.0.0
regime: team
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
  - loop:
      id: build-review
      max_iterations: 3
      until: "review.open_blocking == 0"
      steps:
        - id: develop
          agent: dev
        - id: review
          agent: reviewer
  - gate: review-approval
    approver_role: approver
    four_eyes: true
"""

SCRIPT = {
    "ba": [answer("requirements")],
    "designer": [answer("design")],
    "dev": [answer("dev-report"), answer("dev-report")],
    "reviewer": [review(["no timeout on the export job"]), review()],
}
CALL_USD = Decimal("0.01")


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def test_feature_delivery_runs_end_to_end(tmp_path: Path, fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    layer, repo = tmp_path / "layer", tmp_path / "repo"
    repo.mkdir()
    for name, extra in AGENTS.items():
        write(
            layer / "agents" / f"{name}.md",
            f"---\nname: {name}\n{extra}version: 1.0.0\ndescription: {name}\n---\nGo.\n",
        )
    write(layer / "pipelines" / "feature-delivery.yaml", FEATURE_DELIVERY)
    definitions = DefinitionSet.discover(org=None, team=None, repo=layer, allow_repo_pipelines=True)
    context = ResolveContext(team="payments")

    report = preflight(definitions.pipeline("feature-delivery"), definitions, policy, context)
    assert report.passed, report.text()

    calls = {"n": 0}
    turns: dict[str, int] = {}

    def model_for(step):
        name = step.agent.definition.name

        def respond(messages, info):
            index = turns.get(name, 0)
            turns[name] = index + 1
            calls["n"] += 1
            cost = CallCost(
                source="reported",
                cost_usd=CALL_USD,
                tokens_in=1,
                tokens_out=1,
                tokens_cache_read=None,
                tokens_cache_write=None,
                model="m",
                request_id=None,
                trace_id=None,
            )
            step.budget.reserve(CALL_USD).settle(cost)
            return ModelResponse(parts=[TextPart(SCRIPT[name][min(index, len(SCRIPT[name]) - 1)])])

        return FunctionModel(respond)

    ledgers: dict[str, RunLedger] = {}

    def recorder_for(run_id):
        if run_id not in ledgers:
            ledger = RunLedger(repo, run_id)
            ledger.open(
                identity=RunIdentity(team="payments", policy_version=policy.version),
                kind="feature",
                project="demo",
                owner="payments",
                criticality="low",
            )
            ledgers[run_id] = ledger
        return ledgers[run_id]

    runners = PydanticStepRunners(
        identity_for=lambda step: None,
        gateway=GatewaySettings(base_url="https://tokenlean.example", api_key="k"),
        prices=lambda model: None,
        fallback_estimate_usd=Decimal("0.01"),
        tools_for=lambda step: None,
        state_dir_for=lambda step: tmp_path / "agents" / f"{step.step}-{step.iteration}",
        model_for=model_for,
        policy_version=policy.version,
    )
    engine = Engine(
        definitions=definitions,
        policy=policy,
        context=context,
        store=RunStore(repo / ".agentic" / "local" / "runs"),
        recorder_for=recorder_for,
        gates_for=lambda run_id: ZyrofloGates(recorder_for(run_id)),
        runner_factory=runners,
        block_on_for=regime_block_on,
        clock=FakeClock(),
        run_budget_usd=Decimal("10"),
    )

    def decide(decision):
        return asyncio.run(
            engine.decide(
                "r-e2e",
                decision,
                authorizer=StandaloneAuthorizer(),
                self_approval=self_approval_refusal,
            )
        )

    state = asyncio.run(
        engine.start("feature-delivery", {"request": "Add CSV export"}, "dev@acme.example", "r-e2e")
    )
    assert (state.status, state.pending.id) == ("awaiting_gate", "design-approval")

    state = decide(Approve("design-approval", "lead@acme.example"))
    assert (state.status, state.pending.id) == ("awaiting_gate", "review-approval")
    assert [r.step for r in state.history] == [
        "requirements",
        "design",
        "develop",
        "review",
        "develop",
        "review",
    ]

    state = decide(Approve("review-approval", "reviewer@acme.example"))
    assert state.status == "completed"

    ledger = ledgers["r-e2e"]
    assert ledger.verify() == []  # events.jsonl and approvals.jsonl both chain
    for kind, rel in state.latest.items():
        handoff = parse_handoff(
            (ledger.run_dir / rel).read_text(encoding="utf-8"),
            definitions.handoffs,
            block_on=regime_block_on("team"),
        )
        assert handoff.envelope.kind == kind and handoff.envelope.run_id == "r-e2e"
    assert BudgetLedger.restore(state.budget).run.total_spent() == CALL_USD * calls["n"]
    approvals = (ledger.run_dir / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(approvals) == 2


def test_four_eyes_is_enforced_by_zyroflos_rule():
    assert "may not also approve" in self_approval_refusal("dev@acme.example", "DEV@acme.example")
