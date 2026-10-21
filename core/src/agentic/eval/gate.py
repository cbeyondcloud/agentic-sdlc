"""The agent quality gate (M1-64; DF-243): eval reports against policy targets.

`evaluate_gate` takes one report per agent - written by `agentic eval run` on the holdout
corpus - and compares each metric with its target. Targets come from policy
`eval.targets` (by agent, '*' for every agent); an agent named there replaces its
defaults, which are the M1-64 initial targets:

  * every agent      schema_valid >= 1.0 (100 % schema-valid, after the contract's retry)
  * BA and Design    median blind rubric >= 7/10
  * Developer        tests_pass >= 0.5 (first attempt)
  * Code Review      defect_recall >= 0.7 and false_finding_rate <= 0.3
  * QA               qa_catch >= 0.8

A gate is decided on the point estimate with the 95 % CI shown (S10). Its verdict:

  * NOT_A_GATE - some report cannot decide a gate: not live, not the holdout split, a
                 tag subset, not MEASURED, another corpus than the rest, or two reports
                 for one agent;
  * FAIL       - a measured target is missed;
  * INCOMPLETE - nothing is missed but a target could not be measured (no report for the
                 agent, or a metric the harness does not compute yet);
  * PASS       - every target is met.

The median cost per run of each agent is recorded as the baseline M1-64 asks for.
"""

from collections.abc import Mapping, Sequence
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agentic.eval.report import EvalReport
from agentic.policy import EvalTarget

GateVerdict = Literal["PASS", "FAIL", "INCOMPLETE", "NOT_A_GATE"]
RowVerdict = Literal["PASS", "FAIL", "NOT_MEASURED"]

DEFAULT_TARGETS: dict[str, list[EvalTarget]] = {
    "*": [EvalTarget(metric="schema_valid", at_least=1.0)],
    "business-analyst": [EvalTarget(metric="rubric", statistic="median", at_least=7.0)],
    "design": [EvalTarget(metric="rubric", statistic="median", at_least=7.0)],
    "developer": [EvalTarget(metric="tests_pass", at_least=0.5)],
    "code-review": [
        EvalTarget(metric="defect_recall", at_least=0.7),
        EvalTarget(metric="false_finding_rate", at_most=0.3),
    ],
    "qa": [EvalTarget(metric="qa_catch", at_least=0.8)],
}
GATE_AGENTS = ("business-analyst", "design", "developer", "code-review", "qa")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GateRow(_Model):
    agent: str
    metric: str
    statistic: str
    target: str  # e.g. ">= 7.0"
    value: float | None
    ci_low: float | None = None
    ci_high: float | None = None
    n_scored: int = 0
    verdict: RowVerdict
    note: str = ""


class GateReport(_Model):
    schema_version: Literal[1] = 1
    verdict: GateVerdict
    corpus_manifest_sha: str | None
    reports: dict[str, str]  # agent -> report id
    rows: list[GateRow]
    median_cost_usd: dict[str, float | None]
    problems: list[str]


def targets_for(agent: str, targets: Mapping[str, Sequence[EvalTarget]]) -> list[EvalTarget]:
    """The agent's own targets (policy's, else the defaults) plus the '*' targets."""
    own = targets.get(agent, DEFAULT_TARGETS.get(agent, []))
    shared = targets.get("*", DEFAULT_TARGETS["*"])
    return [*own, *[t for t in shared if t.metric not in {o.metric for o in own}]]


def evaluate_gate(
    reports: Sequence[EvalReport],
    *,
    targets: Mapping[str, Sequence[EvalTarget]] | None = None,
    agents: Sequence[str] = GATE_AGENTS,
) -> GateReport:
    """Compare `reports` with the targets for `agents` (every agent with a report counts)."""
    targets = targets or {}
    problems = _problems(reports)
    by_agent = {r.agent: r for r in reports}
    rows: list[GateRow] = []
    for agent in dict.fromkeys([*agents, *by_agent]):
        for target in targets_for(agent, targets):
            rows.append(_row(agent, target, by_agent.get(agent)))
    if problems:
        verdict: GateVerdict = "NOT_A_GATE"
    elif any(r.verdict == "FAIL" for r in rows):
        verdict = "FAIL"
    elif any(r.verdict == "NOT_MEASURED" for r in rows):
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    shas = sorted({r.corpus_manifest_sha for r in reports})
    return GateReport(
        verdict=verdict,
        corpus_manifest_sha=shas[0] if len(shas) == 1 else None,
        reports={r.agent: r.report_id for r in reports},
        rows=rows,
        median_cost_usd={r.agent: _median_cost(r) for r in reports},
        problems=problems,
    )


def render_gate(gate: GateReport) -> str:
    lines = [
        "# Agent quality gate",
        "",
        f"- **Verdict:** {gate.verdict}",
        f"- **Corpus:** `{gate.corpus_manifest_sha or 'mixed'}`",
        f"- **Reports:** {', '.join(f'{a} `{r}`' for a, r in gate.reports.items()) or 'none'}",
        "",
        "| agent | metric | target | value | 95% CI | cases | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in gate.rows:
        shown = "-" if row.value is None else f"{row.value:.3f}"
        ci = "" if row.ci_low is None else f"[{row.ci_low:.3f}, {row.ci_high:.3f}]"
        what = row.metric if row.statistic == "value" else f"{row.metric} ({row.statistic})"
        verdict = row.verdict + (f" ({row.note})" if row.note else "")
        lines.append(
            f"| {row.agent} | {what} | {row.target} | {shown} | {ci} | {row.n_scored} | {verdict} |"
        )
    lines += ["", "## Median cost per run (baseline)", ""]
    for agent, cost in gate.median_cost_usd.items():
        lines.append(f"- {agent}: {'unmeasured' if cost is None else f'${cost:.4f}'}")
    if gate.problems:
        lines += ["", "## Why this is not a gate", ""] + [f"- {p}" for p in gate.problems]
    return "\n".join(lines) + "\n"


def _problems(reports: Sequence[EvalReport]) -> list[str]:
    problems = []
    seen: set[str] = set()
    for r in reports:
        name = f"{r.agent} ({r.report_id})"
        if r.agent in seen:
            problems.append(f"two reports for {r.agent}: give one per agent")
        seen.add(r.agent)
        if r.mode != "live":
            problems.append(f"{name} ran in {r.mode} mode; a gate needs live runs")
        if r.split != "holdout":
            problems.append(f"{name} ran on the {r.split} split; a gate uses the holdout split")
        if r.tag is not None:
            problems.append(f"{name} ran on the tag subset '{r.tag}', not the whole split")
        if r.verdict != "MEASURED":
            problems.append(f"{name} is {r.verdict}, not MEASURED")
    if len({r.corpus_manifest_sha for r in reports}) > 1:
        problems.append("the reports were measured on different corpora (corpus manifest sha)")
    return problems


def _row(agent: str, target: EvalTarget, report: EvalReport | None) -> GateRow:
    bound = f">= {target.at_least}" if target.at_least is not None else f"<= {target.at_most}"
    base = {"agent": agent, "metric": target.metric, "statistic": target.statistic}
    if report is None:
        return GateRow(**base, target=bound, value=None, verdict="NOT_MEASURED", note="no report")
    aggregate = next(iter(report.aggregates.values()), {}).get(target.metric)
    value = None
    if aggregate is not None:
        value = aggregate.median if target.statistic == "median" else aggregate.value
    if value is None:
        note = "not computed" if aggregate is None else "no case scored"
        return GateRow(**base, target=bound, value=None, verdict="NOT_MEASURED", note=note)
    met = value >= target.at_least if target.at_least is not None else value <= target.at_most
    return GateRow(
        **base,
        target=bound,
        value=value,
        ci_low=aggregate.ci_low,  # type: ignore[union-attr]
        ci_high=aggregate.ci_high,  # type: ignore[union-attr]
        n_scored=aggregate.n_scored,  # type: ignore[union-attr]
        verdict="PASS" if met else "FAIL",
    )


def _median_cost(report: EvalReport) -> float | None:
    costs = [r.cost_usd for r in report.runs if r.cost_usd is not None]
    return median(costs) if costs else None


__all__ = [
    "DEFAULT_TARGETS",
    "GATE_AGENTS",
    "GateReport",
    "GateRow",
    "evaluate_gate",
    "render_gate",
    "targets_for",
]
