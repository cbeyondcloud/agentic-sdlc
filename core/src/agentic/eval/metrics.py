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

TOLERANCE = 3  # lines either side of a seeded range (S10)
SCORED_STATUSES = ("completed", "invalid")  # the agent answered; anything else is unscored


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
    caught: set[int] = set()
    unmatched = []
    for index, raw in enumerate(findings):
        found = _as_dict(raw)
        file, line = found.get("file"), found.get("line")
        hits = set()
        if isinstance(file, str) and isinstance(line, int):
            path = _posix(file)
            category = found.get("category")
            for d_index, defect in enumerate(seeded):
                start, end = defect.lines
                same_place = (
                    _posix(defect.file) == path and start - tolerance <= line <= end + tolerance
                )
                same_kind = not category or str(category).casefold() == defect.category.casefold()
                if same_place and same_kind:
                    hits.add(d_index)
        caught |= hits
        if not hits:
            unmatched.append(index)
    return Matching(frozenset(caught), tuple(unmatched))


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
    if record.status not in SCORED_STATUSES:
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
    attached to the record (per criterion, evidence) for the report and calibration."""

    judges: Mapping[str, RubricJudge] = field(default_factory=dict)  # rubric id -> judge
    scrubber: Scrubber = field(default_factory=Scrubber)
    reference: Callable[[EvalCase], str | None] = lambda case: None

    async def evaluate(
        self, ctx: EvaluatorContext[RunRequest, RunRecord, None]
    ) -> dict[str, float]:
        case, record = ctx.inputs.case, ctx.output
        judge = self.judges.get(case.rubric or "")
        if judge is None or record.status not in SCORED_STATUSES:
            return {}
        if parse_document(record.output, ctx.inputs.kind) is None or record.status != "completed":
            return {"rubric": 0.0}  # an invalid document delivers nothing to grade
        result = await judge.score(
            self.scrubber.scrub(record.output or ""),
            reference=self.reference(case),
            key_points=case.expected.key_points,
        )
        record.judge = result
        return {} if result.total is None else {"rubric": result.total}


def _as_dict(item: object) -> dict:
    if isinstance(item, Mapping):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    return dump() if callable(dump) else {}


def _posix(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")
