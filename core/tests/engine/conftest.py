"""Shared fixtures for the engine tests: fixture agents, a run folder with a real
ledger, an in-memory gate ledger, scripted runners and a clock that tests move."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from agentic.agents.runner import PydanticAIRunner
from agentic.definitions import DefinitionSet
from agentic.engine.engine import Engine, StepContext
from agentic.engine.state import RunStore
from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.policy import ResolveContext, load_policy
from agentic.yamlio import dump_yaml, load_yaml, split_front_matter
from sdlc_commons.ledger import RunIdentity, RunLedger

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

AGENTS = {
    "ba": "type: business-analyst\n",
    "designer": "type: general\nproduces: design\nconsumes: [requirements]\n",
    "dev": "type: developer\nconsumes: [design, review]\n",
    "reviewer": "type: general\nproduces: review\nconsumes: [dev-report]\n",
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def answer(kind: str, **fields) -> str:
    """A valid answer for `kind`: its template with some fields replaced."""
    header, body = split_front_matter(render_template(BUILTIN_CATALOG.schema(kind)))
    values, _ = load_yaml(header)
    values.update(fields)
    return f"---\n{dump_yaml(values)}---\n{body}\n"


def review(open_findings: list[str] | None = None) -> str:
    """A review answer with these open high findings (none: a clean review)."""
    findings = [
        {"severity": "high", "issue": issue, "status": "open"} for issue in open_findings or []
    ]
    return answer("review", findings=findings, summary="reviewed")


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, minutes: float) -> None:
        self.current += timedelta(minutes=minutes)


class FakeGates:
    """The engine's GateLedger in memory: entries pin digests; a changed file voids them."""

    def __init__(self, recorder: RunLedger) -> None:
        self.recorder, self.entries = recorder, []

    def record(self, gate, verdict, approver, artefacts, *, step, notes="", extra=None) -> str:
        self.entries.append(
            {
                "gate": gate,
                "verdict": verdict,
                "approver": approver,
                "artifacts": dict(artefacts),
                "step": step,
                "notes": notes,
                "extra": extra or {},
            }
        )
        return f"sha256:{len(self.entries):064d}"

    def state(self, gate) -> tuple[bool, str]:
        mine = [e for e in self.entries if e["gate"] == gate]
        if not mine:
            return False, f"{gate} has no entry"
        last = mine[-1]
        if last["verdict"] != "approve":
            return False, f"{gate} rejected by {last['approver']}"
        changed = [
            rel
            for rel, digest in last["artifacts"].items()
            if not (self.recorder.run_dir / rel).is_file() or self.recorder.digest(rel) != digest
        ]
        if changed:
            return False, f"changed since {gate} approved it: {', '.join(changed)}"
        return True, ""

    def waiting(self, gate, step) -> None:
        self.recorder.record("gate_waiting", step, gate=gate, hard=True)


Behaviour = str | Callable[[StepContext], str]


@dataclass
class ScriptedRunners:
    """A StepRunnerFactory whose agents answer from a script, in order, per agent."""

    state_root: Path
    script: dict[str, list[Behaviour]]
    prompts: dict[str, list[str]] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    tools: list = field(default_factory=list)

    def __call__(self, ctx: StepContext) -> PydanticAIRunner:
        name = ctx.agent.definition.name

        def respond(messages, info):
            self.prompts.setdefault(name, []).append(_prompt_of(messages))
            answers = self.script[name]
            index = self.calls.get(name, 0)
            self.calls[name] = index + 1
            behaviour = answers[min(index, len(answers) - 1)]
            text = behaviour(ctx) if callable(behaviour) else behaviour
            return ModelResponse(parts=[TextPart(text)])

        return PydanticAIRunner(
            ctx.agent,
            FunctionModel(respond),
            budget=ctx.budget,
            state_dir=self.state_root / f"{ctx.step}-{ctx.iteration}",
            tools=self.tools,
            contract=ctx.contract,
            run_id=ctx.run_id,
            stuck_check=lambda messages: None,
        )


def _prompt_of(messages) -> str:
    for message in messages:
        for part in getattr(message, "parts", []):
            if type(part).__name__ == "UserPromptPart":
                return str(part.content)
    return ""


@dataclass
class Harness:
    repo: Path
    engine: Engine
    store: RunStore
    runners: ScriptedRunners
    clock: FakeClock
    gates: dict[str, FakeGates]
    ledgers: dict[str, RunLedger]

    def ledger(self, run_id: str) -> RunLedger:
        return self.ledgers[run_id]


@pytest.fixture
def make_harness(tmp_path: Path, fixtures: Path):
    """make_harness(pipeline_yaml, script, **engine_kwargs) -> Harness."""
    policy = load_policy(fixtures / "policy" / "policy")
    repo = tmp_path / "repo"
    repo.mkdir()
    layer = tmp_path / "layer"
    for name, extra in AGENTS.items():
        write(
            layer / "agents" / f"{name}.md",
            f"---\nname: {name}\n{extra}version: 1.0.0\ndescription: {name}\n---\n"
            f"You are {name}.\n",
        )

    def build(
        pipeline_yaml: str,
        script: dict[str, list[Behaviour]],
        *,
        clock=None,
        shared: dict | None = None,
        **engine_kwargs,
    ) -> Harness:
        write(layer / "pipelines" / "test.yaml", pipeline_yaml)
        definitions = DefinitionSet.discover(
            org=None, team=None, repo=layer, allow_repo_pipelines=True
        )
        state = shared if shared is not None else {"ledgers": {}, "gates": {}}
        clock = clock or FakeClock()

        def recorder_for(run_id: str) -> RunLedger:
            if run_id not in state["ledgers"]:
                ledger = RunLedger(repo, run_id)
                if (ledger.folder / "events.jsonl").is_file():
                    ledger.resume()
                else:
                    ledger.open(
                        identity=RunIdentity(team="payments", policy_version=policy.version),
                        kind="feature",
                        project="demo",
                        owner="platform",
                        criticality="low",
                    )
                state["ledgers"][run_id] = ledger
            return state["ledgers"][run_id]

        def gates_for(run_id: str) -> FakeGates:
            if run_id not in state["gates"]:
                state["gates"][run_id] = FakeGates(recorder_for(run_id))
            return state["gates"][run_id]

        runners = ScriptedRunners(tmp_path / "agent-state", script)
        store = RunStore(repo / ".agentic" / "local" / "runs")
        engine = Engine(
            definitions=definitions,
            policy=policy,
            context=ResolveContext(team="payments"),
            store=store,
            recorder_for=recorder_for,
            gates_for=gates_for,
            runner_factory=runners,
            block_on_for=lambda regime: frozenset({"critical", "high"}),
            clock=clock,
            **engine_kwargs,
        )
        return Harness(repo, engine, store, runners, clock, state["gates"], state["ledgers"])

    return build


def charge(usd: str) -> Callable[[StepContext], None]:
    """Spend `usd` from the step's budget slice, as a metered call would."""
    from agentic.transport.cost import CallCost

    def spend(ctx: StepContext) -> None:
        amount = Decimal(usd)
        cost = CallCost(
            source="reported",
            cost_usd=amount,
            tokens_in=1,
            tokens_out=1,
            tokens_cache_read=None,
            tokens_cache_write=None,
            model="m",
            request_id=None,
            trace_id=None,
        )
        ctx.budget.reserve(amount).settle(cost)

    return spend
