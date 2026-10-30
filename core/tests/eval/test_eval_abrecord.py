"""The gateway optimisation opt-in A/B (M3-24, TokenLean ST-7; spike S10, decision D41):
S10's non-inferiority plan per agent, the record template, and a dry run of the A/B on
the recorded fixture cassettes - no model is called."""

import asyncio
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from agentic.eval.abrecord import BASELINE, optimisation_plan, render_ab_record
from agentic.eval.abtest import Calibration, decide, run_ab
from agentic.eval.corpus import load_corpus, rubric_path
from agentic.eval.records import RunRecord, RunRequest

REPO = Path(__file__).resolve().parents[3]
CORPUS = REPO / "library" / "eval"
CASSETTES = CORPUS / "fixtures" / "cassettes"
RECORD = REPO / "docs" / "runbooks" / "optimisation-opt-in" / "dry-run-record.md"
SHA = "sha256:" + "c" * 64
GROUP = "G14_tool_output"
#: The stand-in for a group that saves tokens without changing the answer (dry run only).
DRY_RUN_COST_FACTOR = 0.8
NOTES = (
    "Both variants replay the recorded fixture answers (`library/eval/fixtures/cassettes`, "
    "the business-analyst holdout cases tagged `fixture`) and the recorded judge scores. "
    "B's gateway cost is the recorded cost times 0.8, a stand-in for a group that saves "
    "tokens without changing the answer. Three cases cannot reach p < 0.05 (the smallest "
    "sign-flip p is 1/8), so the verdict is NO_BENEFIT: a real record needs S10's 30 cases."
)


def test_plans_follow_s10_per_agent():
    ba = optimisation_plan(
        group=GROUP,
        agent="business-analyst",
        corpus_manifest_sha=SHA,
        seed=1,
        judge_model="family-b/judge",
        rubric_sha=SHA,
    )
    assert (ba.primary_metric, ba.direction, ba.margin) == ("rubric", "non_inferiority", -0.5)
    assert (ba.n, ba.k, ba.budget_usd, ba.pairwise_required) == (30, 3, 100, True)
    assert ba.guards.require_cost_decrease  # the cost must actually fall
    assert ba.variants["A"].name == BASELINE and ba.variants["B"].name == GROUP
    assert ba.variants["B"].profile == f"agentic+{GROUP}"
    dev = optimisation_plan(group=GROUP, agent="developer", corpus_manifest_sha=SHA, seed=1)
    assert (dev.primary_metric, dev.margin, dev.n, dev.budget_usd) == ("tests_pass", -0.15, 40, 750)
    assert dev.judge_model is None and not dev.pairwise_required


def test_a_given_size_is_used_as_given():
    with pytest.raises(ValidationError):  # 0 cases is refused, never the default
        optimisation_plan(group=GROUP, agent="developer", corpus_manifest_sha=SHA, seed=1, n=0)


def test_a_plan_is_refused_without_what_it_needs():
    with pytest.raises(ValueError, match="no S10 non-inferiority margin"):
        optimisation_plan(group=GROUP, agent="qa", corpus_manifest_sha=SHA, seed=1)
    with pytest.raises(ValidationError, match="judge model and rubric sha"):
        optimisation_plan(group=GROUP, agent="design", corpus_manifest_sha=SHA, seed=1)
    with pytest.raises(ValueError, match="name the optimisation group"):
        optimisation_plan(group=" ", agent="developer", corpus_manifest_sha=SHA, seed=1)


CASES = [f"c{i:02d}" for i in range(40)]


def _per_case(level: float, noise: float, seed: int) -> dict:
    rng = random.Random(seed)
    return {c: [level + rng.gauss(0, noise) for _ in range(3)] for c in CASES}


@pytest.mark.parametrize(
    ("quality_b", "cost_b", "verdict", "reason"),
    [
        (1.0, 0.8, "PROMOTE", "every condition"),  # same quality, cheaper: turn it on
        (1.0, 1.0, "NO_BENEFIT", "cost_decrease"),  # no saving: nothing to gain
        (0.7, 0.8, "NO_BENEFIT", "sign-flip"),  # 30 pp worse: cheaper is not enough
    ],
)
def test_the_developer_decision(quality_b, cost_b, verdict, reason):
    plan = optimisation_plan(group=GROUP, agent="developer", corpus_manifest_sha=SHA, seed=3)
    a = _per_case(1.0, 0.05, seed=1)
    b = _per_case(quality_b, 0.05, seed=2)
    costs_a = {c: [1.0, 1.1, 0.9] for c in CASES}
    costs_b = {c: [v * cost_b for v in costs_a[c]] for c in CASES}
    result = decide(plan, a, b, costs_a=costs_a, costs_b=costs_b)
    assert result.verdict == verdict and reason in " ".join(result.reasons)
    record = render_ab_record(plan, result, group=GROUP, date="2026-10-30", dry_run=False)
    assert f"**{verdict}**" in record and plan.sha in record
    assert ("may be turned on" in record) is (verdict == "PROMOTE")


def test_a_record_is_refused_for_another_plans_result():
    plan = optimisation_plan(group=GROUP, agent="developer", corpus_manifest_sha=SHA, seed=3)
    other = optimisation_plan(group="G8_tools", agent="developer", corpus_manifest_sha=SHA, seed=3)
    result = decide(other, {"c": [1.0]}, {"c": [1.0]}, costs_a={}, costs_b={})
    with pytest.raises(ValueError, match="not this plan's"):
        render_ab_record(plan, result, group=GROUP, date="2026-10-30", dry_run=True)


# --- dry run on the recorded fixture cassettes ----------------------------------------


def _calls(case_id: str, repeat: int, name: str) -> list[dict]:
    path = CASSETTES / case_id / "X" / str(repeat) / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))["calls"]


def cassette_variant(cost_factor: float):
    """A variant that replays the recorded agent answer of each case and repeat, with the
    gateway's recorded cost times `cost_factor`."""

    async def run(request: RunRequest) -> RunRecord:
        calls = _calls(request.case.id, request.repeat, "agent")
        answers = [json.loads(c["body"])["choices"][0]["message"]["content"] for c in calls]
        cost = sum(float(c["headers"]["x-tokenlean-cost-usd"]) for c in calls)
        output = [text for text in answers if text][-1]
        return RunRecord(status="completed", output=output, cost_usd=cost * cost_factor)

    return run


@dataclass
class RecordedRubric(Evaluator[RunRequest, RunRecord, None]):
    """The rubric total the recorded judge gave this case and repeat."""

    def evaluate(self, ctx: EvaluatorContext[RunRequest, RunRecord, None]) -> dict[str, float]:
        [call] = _calls(ctx.inputs.case.id, ctx.inputs.repeat, "judge")
        message = json.loads(call["body"])["choices"][0]["message"]
        scores = json.loads(message["tool_calls"][0]["function"]["arguments"])["scores"]
        return {"rubric": float(sum(s["score"] for s in scores))}


@dataclass
class Verdict:
    score_b: float
    consistent: bool


class IdenticalIsATie:
    """Pairwise stand-in: the two variants replay the same answers, so every comparison is
    a consistent tie (a live A/B uses the harness's PairwiseJudge)."""

    async def compare(self, a: str, b: str, *, reference: str | None = None) -> Verdict:
        assert a == b
        return Verdict(score_b=0.5, consistent=True)


def dry_run():
    corpus = load_corpus(CORPUS, public=True)
    cases = corpus.select(agent="business-analyst", split="holdout", tag="fixture")
    judge, rubric = "judge-model", corpus.manifest.files[rubric_path("ba-requirements")]
    plan = optimisation_plan(
        group=GROUP,
        agent="business-analyst",
        corpus_manifest_sha=corpus.manifest.sha,
        seed=24,
        judge_model=judge,
        rubric_sha=rubric,
        n=len(cases),
        k=2,  # the cassettes hold two repeats
    )
    events: list = []
    calibration = Calibration(verdict="ACCEPTED", judge_models=[judge], rubric_shas=[rubric])
    result, runset = asyncio.run(
        run_ab(
            plan,
            {"A": cassette_variant(1.0), "B": cassette_variant(DRY_RUN_COST_FACTOR)},
            cases,
            kind="requirements",
            record=lambda event, **fields: events.append(event),
            corpus_manifest_sha=corpus.manifest.sha,
            judge_model=judge,
            rubric_sha=rubric,
            evaluators=[RecordedRubric()],
            pairwise_judge=IdenticalIsATie(),
            calibration=calibration,
        )
    )
    return plan, result, events, runset, calibration


@pytest.fixture(scope="module")
def dry():
    return dry_run()


def render_dry(dry) -> str:
    plan, result, _, runset, calibration = dry
    return render_ab_record(
        plan,
        result,
        group=GROUP,
        date="2026-10-30",
        dry_run=True,
        notes=NOTES,
        runset=runset,
        calibration=calibration,
    )


def test_the_dry_run_follows_the_protocol(dry):
    plan, result, events, _, _ = dry
    assert events == ["eval.plan_registered", "eval.ab_decided"]
    assert result.n_scored == plan.n == 3 and result.unscored == []
    assert set(result.per_case_diffs.values()) == {0.0}  # the same answers on both sides
    assert result.guards["cost_decrease"].ok  # the stand-in saving is seen
    # three cases can never reach p < 0.05 (the smallest sign-flip p is 1/8): S10's 30
    # cases are the floor of a real record, and the dry run says so rather than pass
    assert result.verdict == "NO_BENEFIT"
    assert "sign-flip p = 0.1250" in result.reasons[0]


def test_the_record_shows_what_s10_asks_for(dry):
    record = render_dry(dry)
    for line in (
        "| Guards set | invalid runs +1 at most, median cost +25 % at most, cost must fall |",
        "| Spent | 0.0227 of 100 USD |",
        "| Judge calibration | ACCEPTED |",
        "| Blinding leak check | not run |",
        "| Models (resolved) | not recorded |",
        "| Verbosity check | not recorded |",
    ):
        assert line in record, line


def test_the_committed_dry_run_record_matches_the_template(dry):
    """The committed record is the template's output; only the shas may differ (they
    change with the corpus; running this file as a script rewrites the record)."""

    def normal(text: str) -> str:
        return re.sub(r"sha256:[0-9a-f]{64}", "sha256:...", "\n".join(text.splitlines()))

    assert normal(RECORD.read_text(encoding="utf-8")) == normal(render_dry(dry))


if __name__ == "__main__":  # rewrite the committed record after a template change
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_bytes(render_dry(dry_run()).encode("utf-8"))
