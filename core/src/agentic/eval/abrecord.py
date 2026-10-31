"""Gateway optimisation opt-in (M3-24, TokenLean ST-7; spike S10, decision D41).

The gateway runs with every behaviour-changing optimisation group off (its LITE profile).
A group is turned on one at a time, and only after a pre-registered non-inferiority A/B
on the eval harness shows that each agent it touches keeps its quality and that the cost
actually falls (docs/runbooks/optimisation-opt-in.md):

* `optimisation_plan` writes that plan for one agent with S10's defaults: the margin, n
  and k, the budget cap, and the guard that the cost difference's CI lies below zero;
* `render_ab_record` writes the record the procedure keeps, from the plan and the result
  of `run_ab`: every number S10 says a record must show, the unscored cases named beside
  the denominator, and what may be done next.
"""

from collections.abc import Mapping

from agentic.eval.abtest import (
    JUDGED_METRICS,
    REVIEW_METRICS,
    ABPlan,
    ABResult,
    Calibration,
    Guards,
    VariantSpec,
)
from agentic.eval.blind import LeakCheck
from agentic.eval.runner import RunSet

#: S10's non-inferiority settings per agent: primary metric, margin, n, k, budget cap (USD).
#: Rubric agents may lose at most half a rubric point, the Developer 15 points of its
#: tests-pass rate. Other agents have no S10 margin yet: their plan is written by hand.
NON_INFERIORITY: Mapping[str, tuple[str, float, int, int, float]] = {
    "business-analyst": ("rubric", -0.5, 30, 3, 100.0),
    "design": ("rubric", -0.5, 30, 3, 100.0),
    "developer": ("tests_pass", -0.15, 40, 3, 750.0),
}
#: The variant every plan compares against: the gateway as it runs today.
BASELINE = "gateway-current"


def optimisation_plan(
    *,
    group: str,
    agent: str,
    corpus_manifest_sha: str,
    seed: int,
    judge_model: str | None = None,
    rubric_sha: str | None = None,
    baseline_profile: str = "agentic",
    n: int | None = None,
    k: int | None = None,
    budget_usd: float | None = None,
) -> ABPlan:
    """The pre-registered plan for turning `group` on, measured on `agent`: A is the
    gateway as it runs (`baseline_profile`), B the same gateway with the group's overlay.
    `n`, `k` and `budget_usd` replace S10's defaults (a dry run uses fewer cases)."""
    if agent not in NON_INFERIORITY:
        known = ", ".join(sorted(NON_INFERIORITY))
        raise ValueError(f"no S10 non-inferiority margin for {agent!r} (known: {known})")
    if not group.strip():
        raise ValueError("name the optimisation group")
    metric, margin, default_n, default_k, cap = NON_INFERIORITY[agent]
    judged = metric == "rubric"
    return ABPlan(
        agent=agent,
        variants={
            "A": VariantSpec(name=BASELINE, profile=baseline_profile),
            "B": VariantSpec(
                name=group,
                description=f"the gateway with {group} turned on",
                profile=f"{baseline_profile}+{group}",
            ),
        },
        corpus_manifest_sha=corpus_manifest_sha,
        primary_metric=metric,
        direction="non_inferiority",
        margin=margin,
        n=default_n if n is None else n,
        k=default_k if k is None else k,
        judge_model=judge_model if judged else None,
        rubric_sha=rubric_sha if judged else None,
        seed=seed,
        guards=Guards(require_cost_decrease=True),
        budget_usd=cap if budget_usd is None else budget_usd,
        pairwise_required=judged,
    )


def render_ab_record(
    plan: ABPlan,
    result: ABResult,
    *,
    group: str,
    date: str,
    dry_run: bool,
    notes: str = "",
    runset: RunSet | None = None,
    calibration: Calibration | None = None,
    leaks: LeakCheck | None = None,
) -> str:
    """The Markdown record of one opt-in A/B (the procedure's record template). `runset`
    (what `run_ab` returns beside the result) gives the spend and the resolved models,
    `calibration` the judge's human calibration, `leaks` the blinding check the judge fed;
    each one left out is written as not recorded or not run, never guessed."""
    if result.plan_sha != plan.sha:
        raise ValueError("the result is not this plan's: its plan sha differs")
    lines = [f"# Optimisation opt-in record: {group} on {plan.agent}", ""]
    if dry_run:
        lines += [
            "> **DRY RUN.** Recorded or synthetic data, no model was called. This record",
            "> proves the procedure and its template; it is not evidence about the group.",
            "",
        ]
    a, b = plan.variants["A"], plan.variants["B"]
    lines += [
        "## Plan (registered before the first run)",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Date | {date} |",
        f"| Plan sha | `{plan.sha}` |",
        f"| Agent | {plan.agent} |",
        f"| A (current) | {a.name} (profile `{a.profile}`) |",
        f"| B (candidate) | {b.name} (profile `{b.profile}`) |",
        f"| Corpus | `{plan.corpus_manifest_sha}`, split {plan.split} |",
        f"| Primary metric | {plan.primary_metric}, {_direction(plan)} |",
        f"| Cases x repeats | {plan.n} x {plan.k}, seed {plan.seed} |",
        f"| Alpha | {plan.alpha} one-sided |",
        f"| Judge | {plan.judge_model or 'none (measured, not judged)'} |",
        f"| Rubric sha | {_code(plan.rubric_sha)} |",
        f"| Budget cap | {plan.budget_usd:g} USD |",
        f"| Guards set | {_guards_set(plan)} |",
        "",
        "## How it ran",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Spent | {_spent(plan, runset)} |",
        f"| Models (resolved) | {_models(runset)} |",
        f"| Judge calibration | {_calibration(plan, calibration)} |",
        f"| Blinding leak check | {_leaks(leaks)} |",
        "| Verbosity check | not recorded |",
        "",
        "## Verdict",
        "",
        f"**{result.verdict}**",
        "",
        *[f"- {reason}" for reason in result.reasons],
        "",
        "## Primary metric",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Cases scored on both sides | {result.n_scored} of {plan.n} |",
        f"| Unscored | {', '.join(result.unscored) or 'none'} |",
        f"| Mean difference B - A | {_stat(result.bootstrap, 'mean')} |",
        f"| Bootstrap 95 % CI | {_ci(result.bootstrap)} |",
        f"| SD of the differences | {_plain(result.sigma_d)} |",
        f"| Sign-flip p (one-sided, mean - margin > 0) | {_stat(result.signflip, 'p_one_sided')} |",
        f"| Sign test | {_sign(result.sign_test)} |",
        f"| Wilcoxon signed-rank p (two-sided) | {_stat(result.wilcoxon, 'p_two_sided')} |",
        "",
        "## Guards",
        "",
    ]
    if result.guards:
        lines += ["| Guard | Holds | Value | Limit | Note |", "|---|---|---|---|---|"]
        for name, guard in result.guards.items():
            lines.append(
                f"| {name} | {'yes' if guard.ok else 'NO'} | {_num(guard.value)} "
                f"| {_num(guard.limit)} | {guard.note} |"
            )
    else:
        lines.append("Not reached: the verdict was decided before the guards ran.")
    lines += ["", "## Pairwise judgement", ""]
    if result.pairwise:
        pair = result.pairwise
        lines.append(
            f"Win rate of B {pair['win_rate']:.2f} over {pair['n']} comparisons; "
            f"{pair['order_inconsistency']:.0%} of verdicts changed with the order"
            f"{' (unreliable)' if pair['unreliable'] else ''}."
        )
    else:
        lines.append("None made.")
    lines += ["", "## Per-case differences (B - A)", "", "| Case | Difference |", "|---|---|"]
    lines += [f"| {case} | {diff:+.4f} |" for case, diff in sorted(result.per_case_diffs.items())]
    lines += ["", "## Decision", "", _next_step(group, plan, result, dry_run)]
    if notes:
        lines += ["", "## Notes", "", notes.strip()]
    lines += [
        "",
        "## Sign-off",
        "",
        "| Role | Name | Date |",
        "|---|---|---|",
        "| Platform owner (runs the gateway) | | |",
        "| Eval owner (checked the plan and the report) | | |",
        "",
    ]
    return "\n".join(lines)


def _next_step(group: str, plan: ABPlan, result: ABResult, dry_run: bool) -> str:
    if dry_run:
        return "None: a dry run never turns a group on."
    if result.verdict == "PROMOTE":
        return (
            f"{group} may be turned on for {plan.agent} once every other agent it touches "
            "has its own PROMOTE record: add its overlay in staging, watch a week, then "
            "production."
        )
    if result.verdict == "NO_BENEFIT":
        return (
            f"{group} stays off. A re-test is a new, larger plan registered before it runs; "
            "this record stays."
        )
    return f"{group} stays off: the A/B measured nothing usable ({result.verdict}). Fix and re-run."


def _guards_set(plan: ABPlan) -> str:
    guards = plan.guards
    out = [
        f"invalid runs +{guards.max_invalid_increase} at most",
        f"median cost +{guards.max_cost_increase:.0%} at most".replace("%", " %"),
    ]
    if plan.primary_metric in REVIEW_METRICS:
        out.append(f"false findings +{guards.max_false_finding_increase:.0%} at most")
    if guards.require_cost_decrease:
        out.append("cost must fall")
    return ", ".join(out)


def _spent(plan: ABPlan, runset: RunSet | None) -> str:
    if runset is None:
        return "not recorded"
    stopped = " (the cap stopped the runs)" if runset.budget_hit else ""
    return f"{runset.spent_usd:.4f} of {plan.budget_usd:g} USD{stopped}"


def _models(runset: RunSet | None) -> str:
    found = sorted({m for run in runset.runs for m in run.record.model_ids}) if runset else []
    return ", ".join(found) if found else "not recorded"


def _calibration(plan: ABPlan, calibration: Calibration | None) -> str:
    if plan.primary_metric not in JUDGED_METRICS:
        return "not needed (the primary metric is measured, not judged)"
    if calibration is None:
        return "none"
    if not calibration.matches(plan):
        return f"{calibration.verdict}, but for another judge model or rubric"
    return calibration.verdict


def _leaks(leaks: LeakCheck | None) -> str:
    if leaks is None:
        return "not run"
    if leaks.leaks:
        return "LEAK: " + ", ".join(leaks.leaks)
    return f"no leak found in {leaks.inputs_seen} judge inputs"


def _direction(plan: ABPlan) -> str:
    if plan.direction == "non_inferiority":
        return f"non-inferiority, margin {plan.margin:+g}"
    return f"superiority, margin {plan.margin:+g}"


def _code(value: str | None) -> str:
    return f"`{value}`" if value else "none"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def _plain(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _stat(found: dict | None, name: str) -> str:
    return "n/a" if not found else f"{found[name]:.4f}"


def _ci(found: dict | None) -> str:
    return "n/a" if not found else f"[{found['ci_low']:+.4f}, {found['ci_high']:+.4f}]"


def _sign(found: dict | None) -> str:
    if not found:
        return "n/a"
    counts = f"{found['pos']} up, {found['neg']} down, {found['ties']} tied"
    return f"{counts}; p {found['p_two_sided']:.4f}"
