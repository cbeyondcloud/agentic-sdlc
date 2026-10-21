"""Running an evaluation (M1-63): cases x labels x repeats on pydantic-evals, interleaved,
budget-capped, every run kept; and one case run by the real agent runner."""

import asyncio
from decimal import Decimal
from pathlib import Path

from _eval_support import REQUIREMENTS_ANSWER, scoping_case
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentic.definitions import load_agent
from agentic.eval.metrics import ObjectiveMetrics
from agentic.eval.records import RunRecord, RunRequest
from agentic.eval.runner import AgentCaseRunner, run_eval
from agentic.handoffs import BUILTIN_CATALOG
from agentic.policy import ResolveContext, load_policy, resolve_agent
from agentic.transport import BudgetLedger


def recorder(log: list, cost: float | None = 0.5, status: str = "completed"):
    async def run(request: RunRequest) -> RunRecord:
        log.append((request.case.id, request.label, request.repeat))
        return RunRecord(status=status, output=REQUIREMENTS_ANSWER, cost_usd=cost)

    return run


def test_every_case_label_and_repeat_runs_interleaved_in_seeded_order():
    log: list = []
    cases = [scoping_case("c1"), scoping_case("c2")]
    result = asyncio.run(
        run_eval(
            cases,
            {"X": recorder(log), "Y": recorder(log)},
            kind="requirements",
            repeats=3,
            seed=11,
        )
    )
    assert len(result.runs) == 12 and len(log) == 12
    assert {(c, lab, r) for c, lab, r in log} == {
        (c, lab, r) for c in ("c1", "c2") for lab in "XY" for r in range(3)
    }
    # interleaved per case: each case's six runs are adjacent, both labels in each repeat
    assert [c for c, _, _ in log] == ["c1"] * 6 + ["c2"] * 6
    assert len({log[i][1] for i in range(0, 12, 2)}) == 2  # the order is flipped per pair
    again: list = []
    asyncio.run(
        run_eval(
            cases,
            {"X": recorder(again), "Y": recorder(again)},
            kind="requirements",
            repeats=3,
            seed=11,
        )
    )
    assert again == log  # the same seed, the same order


def test_metrics_are_attached_to_each_run():
    result = asyncio.run(
        run_eval(
            [scoping_case("c1")],
            {"X": recorder([])},
            kind="requirements",
            repeats=1,
            seed=0,
            evaluators=[ObjectiveMetrics()],
        )
    )
    (run,) = result.runs
    assert run.request.run_id == "c1.X.0"
    assert run.metrics["schema_valid"] == 0.0  # the answer lacks the envelope: not a handoff
    assert run.metrics["cost_usd"] == 0.5


def test_the_budget_cap_skips_the_remaining_runs():
    log: list = []
    result = asyncio.run(
        run_eval(
            [scoping_case("c1"), scoping_case("c2")],
            {"X": recorder(log, cost=1.0)},
            kind="requirements",
            repeats=2,
            seed=0,
            budget_usd=2.0,
        )
    )
    assert result.budget_hit
    assert len(log) == 2
    assert [r.record.status for r in result.runs] == [
        "completed",
        "completed",
        "skipped",
        "skipped",
    ]


def test_a_run_that_raises_is_kept_as_an_error():
    async def broken(request: RunRequest) -> RunRecord:
        raise RuntimeError("connection reset")

    result = asyncio.run(
        run_eval([scoping_case("c1")], {"X": broken}, kind="requirements", repeats=1, seed=0)
    )
    (run,) = result.runs
    assert run.record.status == "error" and "connection reset" in run.record.error
    assert run.metrics == {}


def test_an_agent_s_own_budget_is_its_failure_the_eval_cap_is_not():
    from agentic.eval.runner import hit_eval_cap
    from agentic.transport import LimitHit

    own = Decimal("1")
    step_hit = LimitHit("step", "c1.X.0", own, Decimal("1.2"))
    assert not hit_eval_cap(step_hit, granted=own, own=own)  # the agent ran away: scored
    assert hit_eval_cap(step_hit, granted=Decimal("0.4"), own=own)  # the cap cut its slice
    assert hit_eval_cap(LimitHit("run", "run", Decimal("5"), Decimal("5.1")), granted=own, own=own)


def resolved_ba(fixtures: Path, tmp_path: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    agent_file = tmp_path / "business-analyst.md"
    agent_file.write_text(
        "---\nname: business-analyst\ntype: business-analyst\nversion: 1.0.0\nowner: eval\n"
        "status: active\ndescription: BA for tests.\ntools: []\nproduces: requirements\n"
        "limits: {budget_usd: 1, max_turns: 5, max_minutes: 5}\n---\nWrite requirements.\n",
        encoding="utf-8",
    )
    resolved, violations = resolve_agent(
        load_agent(agent_file), policy, ResolveContext(team="payments")
    )
    assert resolved is not None, violations
    return resolved


def test_an_errored_run_keeps_what_it_spent(fixtures: Path, tmp_path: Path):
    """DF-260: a run that fails after paid calls still counts their cost."""
    from agentic.transport.cost import CallCost

    def model_for(request, costs, budget):
        def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            paid = CallCost("reported", Decimal("0.25"), 10, 5, None, None, "m", None, None)
            costs.add(paid, Decimal("0.25"))
            raise RuntimeError("upstream reset the connection")

        return FunctionModel(answer)

    runner = AgentCaseRunner(
        resolved_ba(fixtures, tmp_path),
        model_for,
        schema=BUILTIN_CATALOG.schema("requirements"),
        state_root=tmp_path / "state",
        budget=BudgetLedger(Decimal("5")),
    )
    request = RunRequest(scoping_case("c1"), "X", 0, "requirements", "c1.X.0")
    record = asyncio.run(runner(request))
    assert record.status == "error" and "reset" in record.error
    assert record.cost_usd == 0.25 and record.requests == 1


def test_a_judge_budget_stop_marks_the_evaluation_incomplete():
    """DF-260: the judge exhausting its share is the eval cap, not a measured run."""
    from agentic.eval.metrics import RubricMetric
    from agentic.transport import LimitHit

    class BrokeJudge:
        async def score(self, *args, **kwargs):
            raise LimitHit("step", "judge", Decimal("1"), Decimal("1.1"))

    case = scoping_case("c1")
    answer = (
        "---\nkind: requirements\nschema_version: 1\nrun_id: r\nstep: s\nagent: a@1\n"
        "produced_at: 2026-10-21T10:00:00Z\n" + REQUIREMENTS_ANSWER.removeprefix("---\n")
    )

    async def run(request: RunRequest) -> RunRecord:
        return RunRecord(status="completed", output=answer)

    result = asyncio.run(
        run_eval(
            [case],
            {"X": run},
            kind="requirements",
            repeats=1,
            seed=0,
            evaluators=[RubricMetric(judge_for=lambda request: BrokeJudge())],
        )
    )
    (only,) = result.runs
    assert "rubric" not in only.metrics and only.record.judge_stopped
    assert result.budget_hit


def test_agent_case_runner_produces_a_schema_valid_handoff(fixtures: Path, tmp_path: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    agent_file = tmp_path / "business-analyst.md"
    agent_file.write_text(
        "---\nname: business-analyst\ntype: business-analyst\nversion: 1.0.0\nowner: eval\n"
        "status: active\ndescription: BA for tests.\ntools: []\nproduces: requirements\n"
        "limits: {budget_usd: 1, max_turns: 5, max_minutes: 5}\n---\nWrite requirements.\n",
        encoding="utf-8",
    )
    resolved, violations = resolve_agent(
        load_agent(agent_file), policy, ResolveContext(team="payments")
    )
    assert resolved is not None, violations
    prompts: list[str] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(info.instructions or "")
        return ModelResponse(parts=[TextPart(REQUIREMENTS_ANSWER)])

    runner = AgentCaseRunner(
        resolved,
        lambda request, costs, budget: FunctionModel(answer),
        schema=BUILTIN_CATALOG.schema("requirements"),
        state_root=tmp_path / "state",
        budget=BudgetLedger(Decimal("5")),
    )
    case = scoping_case("c1")
    record = asyncio.run(
        runner(RunRequest(case=case, label="X", repeat=0, kind="requirements", run_id="c1.X.0"))
    )
    assert record.status == "completed", record.errors
    assert record.output.startswith("---\nkind: requirements")
    assert record.cost_usd is None  # FunctionModel calls go through no meter: unmeasured
    assert "Write requirements." in prompts[0]
