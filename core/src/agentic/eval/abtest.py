"""A/B tests (M1-63, spike S10 "Pre-registration and decision rule").

An `ABPlan` fixes everything that decides the outcome - variants, corpus, primary
metric and direction, margin, n and k, alpha, judge and rubric, seed, guards, budget -
and its sha256 is recorded (`eval.plan_registered`) BEFORE the first run. An amendment
is a new plan; the old result stays in the record.

| Verdict      | Rule                                                                       |
|--------------|----------------------------------------------------------------------------|
| PROMOTE      | the paired sign-flip test of mean(d - margin) > 0 has p < alpha, every     |
|              | guard holds, and (when required) the pairwise win rate of B is >= 0.5      |
| NO_BENEFIT   | otherwise; an inconclusive result is not "almost promoted": a re-test is a |
|              | new, larger pre-registered plan                                            |
| UNMEASURABLE | fewer than n cases scored on both sides, a blinding leak, or a judge       |
|              | calibration failure: an empty comparison is a failure, never a tie         |
| INCOMPLETE   | the budget cap stopped the runs                                            |

Guards: invalid outcomes not higher in B (by more than `max_invalid_increase` runs);
median cost per run B/A within `max_cost_increase` (an unmeasured cost does not hold);
for Code Review the false-finding rate not more than 10 pp higher; for TokenLean
optimisation groups (non-inferiority) the cost must actually fall.

`run_ab` runs both variants under sealed X/Y labels, scores everything, then reveals
the mapping and decides.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from statistics import fmean, median
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_evals.evaluators import Evaluator

from agentic.eval import stats
from agentic.eval.blind import LeakCheck, Scrubber, assign_labels
from agentic.eval.cases import EvalCase, Split
from agentic.eval.metrics import judge_view, scored
from agentic.eval.runner import RunCase, RunSet, run_eval

Verdict = Literal["PROMOTE", "NO_BENEFIT", "UNMEASURABLE", "INCOMPLETE"]
Record = Callable[..., object]  # record(event, **fields): the ledger
JUDGED_METRICS = frozenset({"rubric"})
UNRELIABLE_ORDER = 0.30  # pairwise verdicts that flip with order above this: judge unreliable


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VariantSpec(_Strict):
    name: str
    description: str = ""
    agent_version: str | None = None
    skills: list[str] = []
    tier: str | None = None
    profile: str | None = None  # a TokenLean optimisation profile


class Guards(_Strict):
    max_invalid_increase: int = Field(default=1, ge=0)  # runs
    max_cost_increase: float = Field(default=0.25, ge=0)  # median cost per run, B/A - 1
    max_false_finding_increase: float = Field(default=0.10, ge=0)  # Code Review only
    require_cost_decrease: bool = False  # TokenLean groups: bootstrap CI upper bound < 0


class ABPlan(_Strict):
    agent: str
    variants: dict[Literal["A", "B"], VariantSpec]  # A = the current default, B = the candidate
    corpus_manifest_sha: str
    split: Split = "holdout"
    primary_metric: str
    direction: Literal["superiority", "non_inferiority"] = "superiority"
    margin: float = 0.0
    n: int = Field(ge=1)  # cases that must be scored on both sides
    k: int = Field(ge=1)  # repeats per case per variant
    alpha: float = Field(default=0.05, gt=0, lt=1)
    judge_model: str | None = None
    rubric_sha: str | None = None
    seed: int
    guards: Guards = Guards()
    budget_usd: float = Field(gt=0)
    pairwise_required: bool = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if set(self.variants) != {"A", "B"}:
            raise ValueError("a plan names variants A and B")
        if self.direction == "non_inferiority" and self.margin >= 0:
            raise ValueError("a non-inferiority plan needs a negative margin (e.g. -0.5)")
        if self.direction == "superiority" and self.margin < 0:
            raise ValueError("a superiority plan's margin is 0 or more")
        if self.primary_metric in JUDGED_METRICS and not (self.judge_model and self.rubric_sha):
            raise ValueError("a judged metric needs the judge model and rubric sha pinned")
        return self

    @property
    def sha(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GuardResult(_Strict):
    ok: bool
    value: float | None
    limit: float | None
    note: str = ""


class ABResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_sha: str
    verdict: Verdict
    reasons: list[str]
    n_scored: int
    unscored: list[str]
    per_case_diffs: dict[str, float]
    signflip: dict | None = None
    bootstrap: dict | None = None
    sign_test: dict | None = None
    wilcoxon: dict | None = None
    pairwise: dict | None = None
    guards: dict[str, GuardResult] = {}
    mapping: dict[str, str] = {}  # X/Y -> A/B, revealed after scoring


class PairwiseComparer(Protocol):
    async def compare(self, a: str, b: str, *, reference: str | None = None) -> object: ...


def register_plan(plan: ABPlan, record: Record) -> str:
    """Pre-register `plan`: its sha goes to the ledger before any run."""
    sha = plan.sha
    record("eval.plan_registered", plan_sha=sha, agent=plan.agent)
    return sha


PerCase = Mapping[str, Sequence[float | None]]


def decide(
    plan: ABPlan,
    a: PerCase,
    b: PerCase,
    *,
    costs_a: PerCase,
    costs_b: PerCase,
    invalid_a: int = 0,
    invalid_b: int = 0,
    pairwise: Sequence[float] | None = None,
    order_inconsistent: int = 0,
    false_rate_a: float | None = None,
    false_rate_b: float | None = None,
    leaks: Sequence[str] = (),
    budget_hit: bool = False,
    calibration_ok: bool | None = None,
    mapping: Mapping[str, str] | None = None,
) -> ABResult:
    """The verdict for per-case primary-metric values of A and B (repeats per case)."""
    diffs, unscored = stats.paired_diffs(a, b)
    ids = sorted(stats.per_case(a).keys() & stats.per_case(b).keys())
    base = {
        "plan_sha": plan.sha,
        "n_scored": len(diffs),
        "unscored": unscored,
        "per_case_diffs": dict(zip(ids, diffs, strict=True)),
        "mapping": dict(mapping or {}),
    }
    if leaks:
        return ABResult(
            **base, verdict="UNMEASURABLE", reasons=[f"blinding leak: {', '.join(leaks)}"]
        )
    if calibration_ok is False:
        return ABResult(**base, verdict="UNMEASURABLE", reasons=["the judge failed calibration"])
    if budget_hit:
        return ABResult(**base, verdict="INCOMPLETE", reasons=["the budget cap stopped the runs"])
    if len(diffs) < plan.n:
        reason = f"{len(diffs)} of {plan.n} cases scored on both sides"
        if unscored:
            reason += f" (unscored: {', '.join(unscored)})"
        return ABResult(**base, verdict="UNMEASURABLE", reasons=[reason])

    test = stats.signflip_test(diffs, margin=plan.margin, seed=plan.seed)
    boot = stats.paired_bootstrap(diffs, alpha=plan.alpha, seed=plan.seed)
    guards = _guards(plan, costs_a, costs_b, invalid_a, invalid_b, false_rate_a, false_rate_b)
    pair = None
    if pairwise is not None and len(pairwise):
        total = len(pairwise)
        inconsistency = order_inconsistent / total
        pair = {
            "win_rate": fmean(pairwise),
            "order_inconsistency": inconsistency,
            "n": total,
            "unreliable": inconsistency > UNRELIABLE_ORDER,
        }
    reasons = []
    if test.p_one_sided >= plan.alpha:
        reasons.append(f"sign-flip p = {test.p_one_sided:.4f} is not below alpha {plan.alpha}")
    reasons += [f"guard '{name}' does not hold: {g.note}" for name, g in guards.items() if not g.ok]
    if plan.pairwise_required:
        if pair is None:
            reasons.append("the plan requires a pairwise judgement and none was made")
        elif pair["unreliable"]:
            share = pair["order_inconsistency"]
            reasons.append(f"pairwise judge unreliable: {share:.0%} of verdicts order-dependent")
        elif pair["win_rate"] < 0.5:
            reasons.append(f"pairwise win rate of B is {pair['win_rate']:.2f}, below 0.5")
    return ABResult(
        **base,
        verdict="NO_BENEFIT" if reasons else "PROMOTE",
        reasons=reasons or ["every condition of the plan holds"],
        signflip=test.as_dict(),
        bootstrap=boot.as_dict(),
        sign_test=stats.sign_test(diffs).as_dict(),
        wilcoxon=stats.wilcoxon_signed_rank(diffs).as_dict(),
        pairwise=pair,
        guards=guards,
    )


def _guards(
    plan: ABPlan,
    costs_a: PerCase,
    costs_b: PerCase,
    invalid_a: int,
    invalid_b: int,
    false_rate_a: float | None,
    false_rate_b: float | None,
) -> dict[str, GuardResult]:
    limits = plan.guards
    out = {
        "invalid": GuardResult(
            ok=invalid_b - invalid_a <= limits.max_invalid_increase,
            value=float(invalid_b - invalid_a),
            limit=float(limits.max_invalid_increase),
            note=f"B had {invalid_b} invalid runs, A {invalid_a}",
        )
    }
    runs_a = [c for values in costs_a.values() for c in values if c is not None]
    runs_b = [c for values in costs_b.values() for c in values if c is not None]
    if not runs_a or not runs_b or median(runs_a) <= 0:
        out["cost"] = GuardResult(
            ok=False,
            value=None,
            limit=limits.max_cost_increase,
            note="cost per run is unmeasured, so its increase is unknown",
        )
    else:
        increase = median(runs_b) / median(runs_a) - 1
        out["cost"] = GuardResult(
            ok=increase <= limits.max_cost_increase,
            value=increase,
            limit=limits.max_cost_increase,
            note=f"median cost per run changed by {increase:+.0%}",
        )
    if limits.require_cost_decrease:
        cost_diffs, _ = stats.paired_diffs(costs_a, costs_b)
        if cost_diffs:
            upper = stats.paired_bootstrap(cost_diffs, seed=plan.seed).ci_high
            out["cost_decrease"] = GuardResult(
                ok=upper < 0, value=upper, limit=0.0, note=f"cost difference CI upper {upper:+.4f}"
            )
        else:
            out["cost_decrease"] = GuardResult(
                ok=False, value=None, limit=0.0, note="cost per case is unmeasured"
            )
    if false_rate_a is not None and false_rate_b is not None:
        rise = false_rate_b - false_rate_a
        out["false_findings"] = GuardResult(
            ok=rise <= limits.max_false_finding_increase,
            value=rise,
            limit=limits.max_false_finding_increase,
            note=f"false-finding rate changed by {rise:+.0%}",
        )
    return out


async def run_ab(
    plan: ABPlan,
    runners: Mapping[str, RunCase],
    cases: Sequence[EvalCase],
    *,
    kind: str,
    record: Record,
    evaluators: Sequence[Evaluator] = (),
    pairwise_judge: PairwiseComparer | None = None,
    scrubber: Scrubber | None = None,
    leaks: LeakCheck | None = None,
    max_concurrency: int = 1,
) -> tuple[ABResult, RunSet]:
    """Register the plan, run A and B blind as X/Y, score, reveal, decide."""
    sha = register_plan(plan, record)
    mapping = assign_labels(["A", "B"], seed=plan.seed)
    labelled = {mapping.label(v): runners[v] for v in ("A", "B")}
    runset = await run_eval(
        cases,
        labelled,
        kind=kind,
        repeats=plan.k,
        seed=plan.seed,
        evaluators=evaluators,
        budget_usd=plan.budget_usd,
        max_concurrency=max_concurrency,
        name=f"ab-{plan.agent}",
    )
    scrubber = scrubber or Scrubber()
    by_label: dict[str, dict] = {label: {} for label in labelled}
    costs: dict[str, dict] = {label: {} for label in labelled}
    invalid = dict.fromkeys(labelled, 0)
    outputs: dict[tuple[str, str, int], str] = {}
    for run in runset.runs:
        label, case_id = run.request.label, run.request.case.id
        if not scored(run.record):
            by_label[label].setdefault(case_id, [])
            continue
        by_label[label].setdefault(case_id, []).append(run.metrics.get(plan.primary_metric))
        costs[label].setdefault(case_id, []).append(run.record.cost_usd)
        invalid[label] += run.record.status == "invalid"
        if run.record.output is not None:
            # without the envelope: it names the agent version, which a variant may change
            shown = scrubber.scrub(judge_view(run.record.output))
            outputs[(label, case_id, run.request.repeat)] = shown

    pairwise_y: list[float] = []
    inconsistent = 0
    if pairwise_judge is not None:
        per_case: dict[str, list[float]] = {}
        for case in sorted(cases, key=lambda c: c.id):
            for repeat in range(plan.k):
                x, y = outputs.get(("X", case.id, repeat)), outputs.get(("Y", case.id, repeat))
                if x is None or y is None:
                    continue
                result = await pairwise_judge.compare(x, y)
                per_case.setdefault(case.id, []).append(result.score_b)  # type: ignore[attr-defined]
                inconsistent += not result.consistent  # type: ignore[attr-defined]
        pairwise_y = [fmean(v) for v in per_case.values()]

    mapping.seal_scores()
    revealed = mapping.reveal()  # X/Y -> A/B
    label_of = {variant: label for label, variant in revealed.items()}
    a, b = label_of["A"], label_of["B"]
    pairwise_b = [s if b == "Y" else 1 - s for s in pairwise_y] if pairwise_judge else None
    result = decide(
        plan,
        by_label[a],
        by_label[b],
        costs_a=costs[a],
        costs_b=costs[b],
        invalid_a=invalid[a],
        invalid_b=invalid[b],
        pairwise=pairwise_b,
        order_inconsistent=inconsistent,
        leaks=leaks.leaks if leaks is not None else (),
        budget_hit=runset.budget_hit,
        mapping=revealed,
    )
    record("eval.ab_decided", plan_sha=sha, verdict=result.verdict)
    return result, runset
