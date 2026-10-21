"""Per-run metrics (M1-63, spike S10 judging table).

Objective truth wherever it exists; a judge only where it does not:

| Agent output    | Metric                                                     | Judge? |
|-----------------|------------------------------------------------------------|--------|
| every agent     | schema_valid (completed, parses as its kind), cost, tokens | no     |
| review          | defect_recall: a finding in the same file, within +/-3     | no     |
|                 | lines of a seeded range (category compared when given);    |        |
|                 | unmatched_blocking: open blocking findings matching none   |        |
| test-report     | qa_catch: seeded failing criteria the report shows failed  | no     |
| dev-report      | tests_pass: every fail-to-pass and pass-to-pass test passes | no     |
| requirements,   | rubric: 0-10, `RubricJudge` on the scrubbed output         | yes    |
| design          |                                                            |        |

A metric that cannot be computed is left out of the run's metrics (unmeasurable),
never set to 0: a run that failed for infrastructure reasons, a cost TokenLean did not
report, tests the run did not execute. An answer the agent got wrong is different: an
invalid document delivers nothing, so it scores 0 on its kind's metric as well as on
schema validity (the agent's failure, not the harness's).

An unmatched finding is not automatically false: real diffs carry real issues. The
false-finding rate needs a judge (or a person) to adjudicate unmatched findings, which
is deferred; `unmatched_blocking` is reported meanwhile.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from agentic.eval.blind import Scrubber
from agentic.eval.cases import EvalCase, SeededDefect
from agentic.eval.judge import RubricJudge
from agentic.eval.records import RunRecord, RunRequest
from agentic.handoffs import BLOCK_ON_DEFAULT, Handoff, HandoffError, parse_handoff
from agentic.handoffs.finalise import FRAMEWORK_FIELDS
from agentic.yamlio import YamlFileError, dump_yaml, load_yaml, split_front_matter

TOLERANCE = 3  # lines either side of a seeded range (S10)
# The agent answered, or failed on its own (turns, time, stuck): scored. An error, a skip,
# a pause or a budget stop is the harness's or a person's doing: unscored.
SCORED_STATUSES = ("completed", "invalid", "stopped")


def scored(record: RunRecord) -> bool:
    """Did this run measure the agent (rather than fail for a harness reason)?"""
    return record.status in SCORED_STATUSES and record.error is None


@dataclass(frozen=True)
class Matching:
    caught: frozenset[int]  # indexes of seeded defects some finding matched
    unmatched: tuple[int, ...]  # indexes of findings that matched no seeded defect


def parse_document(output: str | None, kind: str) -> Handoff | None:
    """The run's handoff, if it is a valid document of `kind`."""
    if not output:
        return None
    try:
        handoff = parse_handoff(output)
    except (HandoffError, ValueError):
        return None
    return handoff if handoff.envelope.kind == kind else None


def schema_valid(status: str, output: str | None, kind: str) -> bool:
    return status == "completed" and parse_document(output, kind) is not None


def match_findings(
    findings: Sequence[object], seeded: Sequence[SeededDefect], *, tolerance: int = TOLERANCE
) -> Matching:
    """A maximum one-to-one matching (Kuhn's augmenting paths): a finding counts for at
    most one seeded defect and a defect for at most one finding, and as many defects as
    possible are matched - whatever order the agent listed its findings in. Candidates
    are tried nearest first (inside the range is distance 0)."""
    candidates: list[list[int]] = []
    for raw in findings:
        found = _as_dict(raw)
        file, line = found.get("file"), found.get("line")
        near: list[tuple[int, int]] = []
        if isinstance(file, str) and isinstance(line, int):
            path, category = _posix(file), found.get("category")
            for d_index, defect in enumerate(seeded):
                if _posix(defect.file) != path:
                    continue
                if category and str(category).casefold() != defect.category.casefold():
                    continue
                start, end = defect.lines
                distance = max(start - line, line - end, 0)
                if distance <= tolerance:
                    near.append((distance, d_index))
        candidates.append([d for _, d in sorted(near)])
    owner: dict[int, int] = {}  # defect index -> finding index

    def claim(finding: int, seen: set[int]) -> bool:
        for defect in candidates[finding]:
            if defect in seen:
                continue
            seen.add(defect)
            if defect not in owner or claim(owner[defect], seen):
                owner[defect] = finding
                return True
        return False

    for index in range(len(candidates)):
        claim(index, set())
    matched = set(owner.values())
    unmatched = tuple(i for i in range(len(candidates)) if i not in matched)
    return Matching(frozenset(owner), unmatched)


def defect_recall(findings: Sequence[object], seeded: Sequence[SeededDefect]) -> tuple[int, int]:
    return len(match_findings(findings, seeded).caught), len(seeded)


def unmatched_blocking(
    findings: Sequence[object],
    seeded: Sequence[SeededDefect],
    block_on: frozenset[str] = BLOCK_ON_DEFAULT,
) -> int:
    matching = match_findings(findings, seeded)
    count = 0
    for index in matching.unmatched:
        found = _as_dict(findings[index])
        count += found.get("status", "open") == "open" and found.get("severity") in block_on
    return count


def qa_catch(
    acceptance_results: Sequence[object], failing_criteria: Sequence[str]
) -> tuple[int, int]:
    """(seeded failing criteria the report marks failed, seeded failing criteria)."""
    failed = {
        str(r.get("criterion_id"))
        for r in map(_as_dict, acceptance_results)
        if r.get("passed") is False
    }
    return sum(1 for c in failing_criteria if c in failed), len(failing_criteria)


def objective_metrics(case: EvalCase, record: RunRecord, kind: str) -> dict[str, float]:
    """Every metric this run can support, by name; unmeasurable ones are absent."""
    if not scored(record):
        return {}
    metrics: dict[str, float] = {}
    document = parse_document(record.output, kind) if record.status == "completed" else None
    metrics["schema_valid"] = 1.0 if document is not None else 0.0
    data = document.data.model_dump() if document is not None else {}
    expected = case.expected
    if expected.seeded_defects:
        findings = data.get("findings", [])
        caught, total = defect_recall(findings, expected.seeded_defects)
        metrics |= {
            "defects_caught": float(caught),
            "defects_total": float(total),
            "defect_recall": caught / total,
            "unmatched_blocking": float(unmatched_blocking(findings, expected.seeded_defects)),
        }
    if expected.failing_criteria:
        caught, total = qa_catch(data.get("acceptance_results", []), expected.failing_criteria)
        metrics |= {
            "criteria_caught": float(caught),
            "criteria_total": float(total),
            "qa_catch": caught / total,
        }
    if expected.tests is not None and record.tests is not None:
        wanted = [*expected.tests.fail_to_pass, *expected.tests.pass_to_pass]
        metrics["tests_pass"] = float(all(record.tests.get(test) is True for test in wanted))
    if record.cost_usd is not None:
        metrics["cost_usd"] = record.cost_usd
    if record.tokens_in is not None and record.tokens_out is not None:
        metrics["tokens"] = float(record.tokens_in + record.tokens_out)
    metrics["duration_s"] = record.duration_s
    return metrics


# ── pydantic-evals evaluators ─────────────────────────────────────────────────


@dataclass
class ObjectiveMetrics(Evaluator[RunRequest, RunRecord, None]):
    def evaluate(self, ctx: EvaluatorContext[RunRequest, RunRecord, None]) -> dict[str, float]:
        return objective_metrics(ctx.inputs.case, ctx.output, ctx.inputs.kind)


@dataclass
class RubricMetric(Evaluator[RunRequest, RunRecord, None]):
    """The rubric score of a scoping case's output, judged blind. The judge's result is
    attached to the record (per criterion, evidence) for the report and calibration.

    `judge_for` gives the judge for one run (None: the case is not rubric-judged), so
    each run's judge calls can be keyed to that run (its cassette, its cost)."""

    judge_for: Callable[[RunRequest], RubricJudge | None] = lambda request: None
    scrubber: Scrubber = field(default_factory=Scrubber)
    reference: Callable[[EvalCase], str | None] = lambda case: None

    async def evaluate(
        self, ctx: EvaluatorContext[RunRequest, RunRecord, None]
    ) -> dict[str, float]:
        case, record = ctx.inputs.case, ctx.output
        judge = self.judge_for(ctx.inputs) if case.rubric else None
        if judge is None or not scored(record):
            return {}
        if parse_document(record.output, ctx.inputs.kind) is None or record.status != "completed":
            return {"rubric": 0.0}  # an invalid document delivers nothing to grade
        record.judged = self.scrubber.scrub(judge_view(record.output or ""))  # what it sees
        result = await judge.score(
            record.judged,
            reference=self.reference(case),
            key_points=case.expected.key_points,
        )
        record.judge = result
        return {} if result.total is None else {"rubric": result.total}


def judge_view(document: str) -> str:
    """The document without the framework's envelope (run id, agent@version, time): those
    are not the agent's work, and an agent version is exactly what an A/B varies."""
    try:
        header, body = split_front_matter(document)
        meta, _ = load_yaml(header)
    except YamlFileError:
        return document
    if not isinstance(meta, dict):
        return document
    kept = {k: v for k, v in meta.items() if k not in FRAMEWORK_FIELDS}
    return f"---\n{dump_yaml(kept)}---\n{body}"


def _as_dict(item: object) -> dict:
    if isinstance(item, Mapping):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    return dump() if callable(dump) else {}


def _posix(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")
