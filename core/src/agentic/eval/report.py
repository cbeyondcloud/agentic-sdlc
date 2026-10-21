"""The eval report (M1-63, spike S10 data model): JSON for machines, Markdown for people.

Aggregates are per label (X, or X and Y) and per metric. The case is the unit: repeats
are averaged within a case first, then

  * a mean metric (rubric, schema_valid, tests_pass, cost) reports the mean and median of
    the case means, with a bootstrap CI over cases;
  * a pooled rate (defect_recall, qa_catch) reports caught / seeded over all cases, with
    a case-cluster bootstrap CI.

Every case a metric could not score is named beside its denominator. The verdict:

  * UNMEASURABLE - a blinding leak, a replay failure, or nothing scored at all;
  * INCOMPLETE   - the budget cap stopped the evaluation;
  * MEASURED     - otherwise. Whether the numbers meet a target is the gate's call (M1-64).

`comparable` is the view CI compares with a committed expected report: everything but
ids, timestamps and durations, which differ from run to run.
"""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from statistics import fmean, median
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agentic.eval import stats
from agentic.eval.runner import EvalRunResult, RunSet

Mode = Literal["live", "replay", "record", "test"]
Verdict = Literal["MEASURED", "UNMEASURABLE", "INCOMPLETE"]
#: Pooled rates: metric -> (numerator, denominator) metrics recorded per run.
RATIO_METRICS = {
    "defect_recall": ("defects_caught", "defects_total"),
    "qa_catch": ("criteria_caught", "criteria_total"),
}
PARTS = {part for pair in RATIO_METRICS.values() for part in pair}
ORDER = ["schema_valid", "rubric", "defect_recall", "unmatched_blocking", "qa_catch"]
ORDER += ["tests_pass", "cost_usd", "tokens", "duration_s"]
VOLATILE = ("duration_s",)
#: The metric each case class exists to measure: always listed, so a metric nobody
#: could compute shows as unscored instead of quietly missing.
EXPECTED = {
    "scoping": "rubric",
    "review": "defect_recall",
    "qa": "qa_catch",
    "dev_task": "tests_pass",
    "defect": "tests_pass",
}
JUDGED = frozenset({"rubric"})  # a judged primary that nobody scored voids the report
VERBOSITY_FLAG = 0.5  # |r| between rubric score and output length above this is flagged


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JudgeRecord(_Model):
    model_id: str
    rubric_key: str
    rubric_sha: str
    per_criterion: dict[str, int | None]
    reasks: int


class EvalRun(_Model):
    run_id: str
    case_id: str
    label: str
    repeat: int
    status: str
    output_sha: str | None
    metrics: dict[str, float]
    judge: JudgeRecord | None = None
    cost_usd: float | None
    cost_estimated: bool
    tokens_in: int | None
    tokens_out: int | None
    model_ids: list[str]
    requests: int
    duration_s: float
    error: str | None
    errors: list[str]
    evaluator_errors: list[str]


class MetricAggregate(_Model):
    value: float | None
    median: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    n_scored: int
    unscored: list[str]  # case ids this metric could not score


class JudgeInfo(_Model):
    tier: str
    model: str
    family: str
    same_family: bool
    rubrics: dict[str, str]  # rubric key -> sha256


class EvalReport(_Model):
    schema_version: Literal[1] = 1
    report_id: str
    created_at: str
    mode: Mode
    agent: str
    agent_version: str | None
    kind: str
    corpus_manifest_sha: str
    split: str
    cases: list[str]
    k: int
    seed: int
    labels: dict[str, str]  # revealed after scoring
    judge: JudgeInfo | None
    runs: list[EvalRun]
    aggregates: dict[str, dict[str, MetricAggregate]]  # label -> metric -> aggregate
    unscored_runs: list[str]
    cost_total_usd: float | None
    cost_unmeasured_runs: int
    model_ids: list[str]
    model_changed: bool
    stale_cassettes: int
    leaks: list[str]
    budget_usd: float | None
    budget_hit: bool
    judge_cost_usd: float | None = None  # the judge's calls, also charged to the budget
    verbosity_r: dict[str, float | None]  # label -> Pearson r(rubric, output length)
    verdict: Verdict
    problems: list[str]

    @property
    def n(self) -> int:
        return len(self.cases)


def build_report(
    runset: RunSet,
    *,
    report_id: str,
    created_at: str,
    mode: Mode,
    agent: str,
    agent_version: str | None,
    kind: str,
    corpus_manifest_sha: str,
    split: str,
    labels: dict[str, str],
    judge: JudgeInfo | None = None,
    leaks: Sequence[str] = (),
    stale_cassettes: int = 0,
    problems: Sequence[str] = (),
    judge_cost_usd: float | None = None,
) -> EvalReport:
    runs = [_eval_run(r) for r in runset.runs]
    cases = sorted({r.case_id for r in runs})
    expected = sorted({EXPECTED[r.request.case.case_class] for r in runset.runs})
    by_label: dict[str, list[EvalRun]] = {}
    for item in runs:
        by_label.setdefault(item.label, []).append(item)
    aggregates = {
        label: _aggregates(items, cases, runset.seed, expected)
        for label, items in sorted(by_label.items())
    }
    problems = list(problems)
    for label, metrics in aggregates.items():
        for name in sorted(JUDGED & set(expected)):
            if metrics[name].n_scored == 0:
                problems.append(
                    f"{label}: no case got a {name} score - the judge failed on every run "
                    "(see the evaluator errors)"
                )
    # every run that spent counts, scored or not: an INCOMPLETE run's spend is real
    spent = [r for r in runs if r.requests or r.cost_usd is not None]
    measured = [r.cost_usd for r in spent if r.cost_usd is not None]
    model_ids = sorted({m for r in runs for m in r.model_ids})
    if leaks or problems or not any(scored_run(r) for r in runs):
        verdict: Verdict = "UNMEASURABLE"
    elif runset.budget_hit:
        verdict = "INCOMPLETE"
    else:
        verdict = "MEASURED"
    return EvalReport(
        report_id=report_id,
        created_at=created_at,
        mode=mode,
        agent=agent,
        agent_version=agent_version,
        kind=kind,
        corpus_manifest_sha=corpus_manifest_sha,
        split=split,
        cases=cases,
        k=runset.repeats,
        seed=runset.seed,
        labels=labels,
        judge=judge,
        runs=runs,
        aggregates=aggregates,
        unscored_runs=[r.run_id for r in runs if not scored_run(r)],
        cost_total_usd=round(sum(measured), 6) if measured else None,
        cost_unmeasured_runs=len(spent) - len(measured),
        model_ids=model_ids,
        # one label's runs answered by more than one model: the provider changed mid-run
        # (two variants on different tiers are expected to differ, so it is per label)
        model_changed=any(
            len({m for r in items for m in r.model_ids}) > 1 for items in by_label.values()
        ),
        stale_cassettes=stale_cassettes,
        leaks=list(leaks),
        budget_usd=runset.budget_usd,
        budget_hit=runset.budget_hit,
        judge_cost_usd=judge_cost_usd,
        verbosity_r={label: _verbosity(items, runset) for label, items in sorted(by_label.items())},
        verdict=verdict,
        problems=problems,
    )


def scored_run(run: EvalRun) -> bool:
    return run.status in ("completed", "invalid", "stopped") and run.error is None


def render_markdown(report: EvalReport) -> str:
    lines = [
        f"# Eval report: {report.agent} ({report.kind})",
        "",
        f"- **Verdict:** {report.verdict}",
        f"- **Mode:** {report.mode} | **split:** {report.split} | "
        f"**cases:** {report.n} x {report.k} repeats | **seed:** {report.seed}",
        f"- **Corpus:** `{report.corpus_manifest_sha}`",
        f"- **Agent:** {report.agent}@{report.agent_version or '?'} | "
        f"**models:** {', '.join(report.model_ids) or 'none recorded'}",
    ]
    if report.judge is not None:
        judge = report.judge
        same = (
            " (**same family as the agent**: calibration sample doubles)"
            if judge.same_family
            else ""
        )
        rubrics = ", ".join(f"{k} `{v[:19]}`" for k, v in judge.rubrics.items())
        lines.append(f"- **Judge:** tier `{judge.tier}`, family {judge.family}{same}; {rubrics}")
    cost = "unmeasured" if report.cost_total_usd is None else f"${report.cost_total_usd:.4f}"
    lines.append(
        f"- **Cost:** {cost} ({report.cost_unmeasured_runs} runs unmeasured)"
        + (f" | budget ${report.budget_usd:.2f}" if report.budget_usd is not None else "")
    )
    for label, metrics in report.aggregates.items():
        variant = report.labels.get(label, label)
        lines += ["", f"## Metrics: {label} ({variant})", ""]
        lines += ["| metric | value | median | 95% CI | cases scored | unscored |"]
        lines += ["|---|---|---|---|---|---|"]
        for name, agg in metrics.items():
            ci = "" if agg.ci_low is None else f"[{_num(agg.ci_low)}, {_num(agg.ci_high)}]"
            lines.append(
                f"| {name} | {_num(agg.value)} | {_num(agg.median)} | {ci} | {agg.n_scored} | "
                f"{', '.join(agg.unscored) or '-'} |"
            )
    lines += ["", "## Runs", "", "| run | status | metrics |", "|---|---|---|"]
    for item in report.runs:
        shown = ", ".join(f"{k}={_num(v)}" for k, v in item.metrics.items() if k not in VOLATILE)
        status = item.status + (f" ({item.error})" if item.error else "")
        lines.append(f"| {item.run_id} | {status} | {shown or '-'} |")
    notes = []
    if report.unscored_runs:
        notes.append(f"Unscored runs: {', '.join(report.unscored_runs)}.")
    if report.leaks:
        notes.append(f"Blinding leak (the evaluation is void): {', '.join(report.leaks)}.")
    if report.problems:
        notes += [f"Problem: {p}" for p in report.problems]
    if report.stale_cassettes:
        notes.append(
            f"{report.stale_cassettes} stale cassettes: prompts changed since recording; "
            "re-record (`--mode record`)."
        )
    errors = [f"{r.run_id}: {e}" for r in report.runs for e in r.evaluator_errors]
    if errors:
        notes.append(f"{len(errors)} evaluator errors: {'; '.join(errors[:5])}")
    if report.model_changed:
        notes.append("The resolved model changed during the evaluation.")
    if report.budget_hit:
        notes.append("The budget cap stopped the evaluation (INCOMPLETE).")
    for label, r in report.verbosity_r.items():
        if r is not None and abs(r) > VERBOSITY_FLAG:
            notes.append(f"Verbosity: rubric score tracks output length for {label} (r={r:.2f}).")
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


def write_report(
    report: EvalReport, folder: Path, runs: Sequence[EvalRunResult] = ()
) -> dict[str, Path]:
    """report.json, report.md, and judged/<run>.md (exactly what the judge saw: the
    calibration export shows people the same text). LF line endings throughout."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = {"json": folder / "report.json", "markdown": folder / "report.md"}
    _write(paths["json"], json.dumps(report.model_dump(mode="json"), indent=2) + "\n")
    _write(paths["markdown"], render_markdown(report))
    for item in runs:
        if item.record.judged is not None:
            _write(folder / "judged" / f"{item.request.run_id}.md", item.record.judged)
    return paths


def comparable(report: EvalReport) -> dict:
    data = report.model_dump(mode="json")
    for key in ("report_id", "created_at"):
        data.pop(key)
    for item in data["runs"]:
        item.pop("duration_s")
        for name in VOLATILE:
            item["metrics"].pop(name, None)
    for metrics in data["aggregates"].values():
        for name in VOLATILE:
            metrics.pop(name, None)
    return data


def _eval_run(result: EvalRunResult) -> EvalRun:
    request, record = result.request, result.record
    judge = None
    if record.judge is not None:
        judge = JudgeRecord(
            model_id=record.judge.model_id,
            rubric_key=record.judge.rubric_key,
            rubric_sha=record.judge.rubric_sha,
            per_criterion=dict(record.judge.per_criterion),
            reasks=record.judge.reasks,
        )
    output_sha = None
    if record.output is not None:
        lf = record.output.replace("\r\n", "\n").encode("utf-8")
        output_sha = "sha256:" + hashlib.sha256(lf).hexdigest()
    return EvalRun(
        run_id=request.run_id,
        case_id=request.case.id,
        label=request.label,
        repeat=request.repeat,
        status=record.status,
        output_sha=output_sha,
        metrics=dict(result.metrics),
        judge=judge,
        cost_usd=record.cost_usd,
        cost_estimated=record.cost_estimated,
        tokens_in=record.tokens_in,
        tokens_out=record.tokens_out,
        model_ids=list(record.model_ids),
        requests=record.requests,
        duration_s=record.duration_s,
        error=record.error,
        errors=list(record.errors),
        evaluator_errors=list(result.evaluator_errors),
    )


def _aggregates(
    runs: list[EvalRun], cases: list[str], seed: int, expected: Sequence[str] = ()
) -> dict[str, MetricAggregate]:
    names = ({name for r in runs for name in r.metrics} - PARTS) | {"schema_valid", *expected}
    ordered = [n for n in ORDER if n in names] + sorted(names - set(ORDER))
    out = {}
    for name in ordered:
        if name in RATIO_METRICS:
            out[name] = _ratio(runs, cases, RATIO_METRICS[name], seed)
        else:
            out[name] = _mean(runs, cases, name, seed)
    return out


def _mean(runs: list[EvalRun], cases: list[str], name: str, seed: int) -> MetricAggregate:
    per_run: dict[str, list[float | None]] = {c: [] for c in cases}
    for r in runs:
        per_run[r.case_id].append(r.metrics.get(name))
    means = stats.per_case(per_run)
    unscored = [c for c in cases if c not in means]
    if not means:
        return MetricAggregate(value=None, n_scored=0, unscored=unscored)
    values = [means[c] for c in sorted(means)]
    boot = stats.paired_bootstrap(values, seed=seed)
    return MetricAggregate(
        value=fmean(values),
        median=median(values),
        ci_low=boot.ci_low,
        ci_high=boot.ci_high,
        n_scored=len(values),
        unscored=unscored,
    )


def _ratio(
    runs: list[EvalRun], cases: list[str], parts: tuple[str, str], seed: int
) -> MetricAggregate:
    numerator, denominator = parts
    pairs = []
    unscored = []
    for case in cases:
        measured = [
            (r.metrics[numerator], r.metrics[denominator])
            for r in runs
            if r.case_id == case and numerator in r.metrics and denominator in r.metrics
        ]
        if measured:
            pairs.append((fmean(n for n, _ in measured), fmean(d for _, d in measured)))
        else:
            unscored.append(case)
    try:
        boot = stats.cluster_bootstrap_ratio(pairs, seed=seed)
    except stats.Unmeasurable:
        return MetricAggregate(value=None, n_scored=0, unscored=unscored)
    return MetricAggregate(
        value=boot.mean,
        ci_low=boot.ci_low,
        ci_high=boot.ci_high,
        n_scored=len(pairs),
        unscored=unscored,
    )


def _verbosity(runs: list[EvalRun], runset: RunSet) -> float | None:
    lengths = {r.request.run_id: len(r.record.output or "") for r in runset.runs}
    rubric = [(r.metrics["rubric"], lengths[r.run_id]) for r in runs if "rubric" in r.metrics]
    if not rubric:
        return None
    return stats.pearson([s for s, _ in rubric], [float(n) for _, n in rubric])


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
