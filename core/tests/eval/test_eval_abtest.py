"""A/B decisions (M1-63, spike S10 "Pre-registration and decision rule")."""

import asyncio
import random
from dataclasses import dataclass

import pytest
from _eval_support import scoping_case
from pydantic import ValidationError
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from agentic.eval.abtest import ABPlan, decide, register_plan, run_ab
from agentic.eval.blind import LeakCheck, Scrubber
from agentic.eval.records import RunRecord, RunRequest

SHA = "sha256:" + "c" * 64


def plan(**overrides) -> ABPlan:
    data = {
        "agent": "business-analyst",
        "variants": {"A": {"name": "baseline"}, "B": {"name": "with-skill", "skills": ["x/y"]}},
        "corpus_manifest_sha": SHA,
        "primary_metric": "rubric",
        "n": 20,
        "k": 3,
        "judge_model": "family-b/judge",
        "rubric_sha": SHA,
        "seed": 7,
        "budget_usd": 100,
        "pairwise_required": False,
    }
    return ABPlan.model_validate({**data, **overrides})


def per_case(values: dict[str, float], noise: float = 0.0, seed: int = 0) -> dict:
    rng = random.Random(seed)
    return {c: [v + rng.gauss(0, noise) for _ in range(3)] for c, v in values.items()}


CASES = [f"c{i:02d}" for i in range(20)]
COSTS = {c: [1.0, 1.0, 1.0] for c in CASES}


def test_plan_sha_is_stable_and_sensitive_to_every_field():
    assert plan().sha == plan().sha
    assert plan(n=21).sha != plan().sha
    assert plan(seed=8).sha != plan().sha


def test_plan_rules():
    with pytest.raises(ValidationError, match="negative margin"):
        plan(direction="non_inferiority", margin=0.0)
    with pytest.raises(ValidationError, match="judge"):
        plan(judge_model=None)
    plan(primary_metric="defect_recall", judge_model=None, rubric_sha=None)


def test_register_records_the_sha():
    events = []
    sha = register_plan(plan(), lambda event, **fields: events.append((event, fields)))
    assert events == [("eval.plan_registered", {"plan_sha": sha, "agent": "business-analyst"})]


def test_a_clear_win_promotes():
    a = per_case({c: 6.0 for c in CASES}, noise=0.3, seed=1)
    b = per_case({c: 7.5 for c in CASES}, noise=0.3, seed=2)
    result = decide(plan(), a, b, costs_a=COSTS, costs_b=COSTS)
    assert result.verdict == "PROMOTE", result.reasons
    assert result.signflip["p_one_sided"] < 0.05
    assert result.bootstrap["ci_low"] > 0 and len(result.per_case_diffs) == 20


def test_noise_shows_no_benefit():
    a = per_case({c: 6.0 for c in CASES}, noise=1.0, seed=1)
    b = per_case({c: 6.0 for c in CASES}, noise=1.0, seed=2)
    assert decide(plan(), a, b, costs_a=COSTS, costs_b=COSTS).verdict == "NO_BENEFIT"


def test_fewer_than_n_cases_scored_is_unmeasurable():
    a = per_case({c: 6.0 for c in CASES[:19]})
    b = per_case({c: 8.0 for c in CASES[:19]})
    result = decide(plan(), a, b, costs_a=COSTS, costs_b=COSTS)
    assert result.verdict == "UNMEASURABLE" and "19 of 20" in result.reasons[0]


def test_leak_and_budget():
    a, b = per_case({c: 6.0 for c in CASES}), per_case({c: 8.0 for c in CASES})
    assert (
        decide(plan(), a, b, costs_a=COSTS, costs_b=COSTS, leaks=["with-skill"]).verdict
        == "UNMEASURABLE"
    )
    assert (
        decide(plan(), a, b, costs_a=COSTS, costs_b=COSTS, budget_hit=True).verdict == "INCOMPLETE"
    )


def test_guards_block_a_win():
    a, b = per_case({c: 6.0 for c in CASES}), per_case({c: 8.0 for c in CASES})
    dear = {c: [1.5, 1.5, 1.5] for c in CASES}
    result = decide(plan(), a, b, costs_a=COSTS, costs_b=dear)
    assert result.verdict == "NO_BENEFIT" and not result.guards["cost"].ok
    result = decide(plan(), a, b, costs_a=COSTS, costs_b=COSTS, invalid_a=0, invalid_b=2)
    assert result.verdict == "NO_BENEFIT" and not result.guards["invalid"].ok
    unmeasured = {c: [None] * 3 for c in CASES}
    result = decide(plan(), a, b, costs_a=unmeasured, costs_b=unmeasured)
    assert not result.guards["cost"].ok and "unmeasured" in result.guards["cost"].note


def test_pairwise_below_half_blocks_a_rubric_win():
    a, b = per_case({c: 6.0 for c in CASES}), per_case({c: 8.0 for c in CASES})
    result = decide(
        plan(pairwise_required=True), a, b, costs_a=COSTS, costs_b=COSTS, pairwise=[0.4] * 20
    )
    assert result.verdict == "NO_BENEFIT" and result.pairwise["win_rate"] == 0.4


def test_non_inferiority_with_a_negative_margin():
    a = per_case({c: 6.0 for c in CASES}, noise=0.2, seed=1)
    b = per_case({c: 5.9 for c in CASES}, noise=0.2, seed=2)
    ni = plan(direction="non_inferiority", margin=-0.5)
    assert decide(ni, a, b, costs_a=COSTS, costs_b=COSTS).verdict == "PROMOTE"
    assert decide(plan(), a, b, costs_a=COSTS, costs_b=COSTS).verdict == "NO_BENEFIT"


@dataclass
class ScoreFromOutput(Evaluator[RunRequest, RunRecord, None]):
    def evaluate(self, ctx: EvaluatorContext[RunRequest, RunRecord, None]) -> dict[str, float]:
        return {"rubric": float(ctx.output.output.split("=")[1])}


def variant(score: float, log: list, tell: str = ""):
    async def run(request: RunRequest) -> RunRecord:
        log.append(("run", request.run_id))
        return RunRecord(status="completed", output=f"{tell}score={score}", cost_usd=0.1)

    return run


def test_plan_registered_before_any_run_and_mapping_revealed_only_in_the_result():
    log: list = []
    cases = [scoping_case(c) for c in CASES[:4]]
    small = plan(n=4, k=2)
    result, runset = asyncio.run(
        run_ab(
            small,
            {"A": variant(5.0, log), "B": variant(8.0, log)},
            cases,
            kind="requirements",
            record=lambda event, **fields: log.append((event, fields.get("plan_sha"))),
            evaluators=[ScoreFromOutput()],
        )
    )
    assert log[0] == ("eval.plan_registered", small.sha)
    assert all(entry[0] == "run" for entry in log[1:-1]) and log[-1][0] == "eval.ab_decided"
    assert {r.request.label for r in runset.runs} == {"X", "Y"}  # runs carry opaque labels
    assert set(result.mapping.values()) == {"A", "B"}
    assert result.plan_sha == small.sha
    assert result.per_case_diffs == {c.id: 3.0 for c in cases}


def test_a_leak_makes_the_ab_unmeasurable():
    from agentic.eval.judge import PairwiseJudge  # noqa: F401 - the judge type it would use

    cases = [scoping_case(c) for c in CASES[:4]]

    class LeakyJudge:
        def __init__(self, leaks):
            self.leaks = leaks

        async def compare(self, a, b, *, reference=None):
            self.leaks.see(a + b)

            class Result:
                score_b, consistent = 0.5, True

            return Result()

    leaks = LeakCheck(["secret-skill"])
    result, _ = asyncio.run(
        run_ab(
            plan(n=4, k=1, pairwise_required=True),
            {"A": variant(5.0, []), "B": variant(8.0, [], tell="secret-skill ")},
            cases,
            kind="requirements",
            record=lambda event, **fields: None,
            evaluators=[ScoreFromOutput()],
            pairwise_judge=LeakyJudge(leaks),
            scrubber=Scrubber(),  # told nothing, so the tell gets through
            leaks=leaks,
        )
    )
    assert result.verdict == "UNMEASURABLE" and "secret-skill" in result.reasons[0]
