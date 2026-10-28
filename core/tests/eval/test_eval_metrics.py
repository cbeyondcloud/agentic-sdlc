"""Objective metrics (M1-63, spike S10 judging table): mechanical truth wherever it exists."""

import pytest

from agentic.eval.cases import EvalCase, SeededDefect
from agentic.eval.metrics import (
    match_findings,
    objective_metrics,
    qa_catch,
    schema_valid,
    unmatched_blocking,
)
from agentic.eval.records import RunRecord

DEFECTS = [
    SeededDefect(file="billing/export.py", lines=(40, 44), category="logic"),
    SeededDefect(file="api/routes.py", lines=(10, 10), category="security"),
]
SOURCE = {"repo": "synthetic", "synthetic": True, "licence": "CC0-1.0"}


def finding(file, line, severity="high", **extra):
    return {"severity": severity, "file": file, "line": line, "issue": "x", **extra}


@pytest.mark.parametrize(
    "line, caught", [(37, True), (40, True), (47, True), (36, False), (48, False)]
)
def test_a_finding_matches_within_three_lines_of_the_range(line, caught):
    result = match_findings([finding("billing/export.py", line)], DEFECTS)
    assert (0 in result.caught) is caught


def test_other_file_or_missing_location_never_matches():
    result = match_findings(
        [finding("billing/other.py", 40), finding(None, None), finding("api/routes.py", None)],
        DEFECTS,
    )
    assert result.caught == frozenset() and result.unmatched == (0, 1, 2)


def test_paths_are_compared_in_posix_form():
    result = match_findings([finding(".\\billing\\export.py", 41)], DEFECTS)
    assert result.caught == {0}


def test_category_is_compared_only_when_the_finding_has_one():
    assert match_findings([finding("api/routes.py", 10, category="logic")], DEFECTS).caught == set()
    assert match_findings([finding("api/routes.py", 10, category="Security")], DEFECTS).caught == {
        1
    }


def test_one_finding_claims_one_defect_the_nearest():
    close = [
        SeededDefect(file="a.py", lines=(2, 3), category="logic"),
        SeededDefect(file="a.py", lines=(4, 4), category="logic"),
    ]
    assert match_findings([finding("a.py", 4)], close).caught == {1}  # not both
    both = match_findings([finding("a.py", 4), finding("a.py", 2)], close)
    assert both.caught == {0, 1} and both.unmatched == ()
    taken = match_findings([finding("a.py", 4), finding("a.py", 4)], close[1:])
    assert taken.caught == {0} and taken.unmatched == (1,)  # a duplicate finding claims nothing


def test_matching_is_maximal_whatever_the_finding_order():
    apart = [
        SeededDefect(file="a.py", lines=(40, 41), category="logic"),
        SeededDefect(file="a.py", lines=(46, 47), category="logic"),
    ]
    # greedy would give line 44 the nearer defect B and leave line 49 with nothing
    for order in (
        [finding("a.py", 44), finding("a.py", 49)],
        [finding("a.py", 49), finding("a.py", 44)],
    ):
        result = match_findings(order, apart)
        assert result.caught == {0, 1} and result.unmatched == ()


def test_unmatched_blocking_counts_open_blocking_findings_only():
    findings = [
        finding("billing/export.py", 41),  # matches
        finding("x.py", 1, severity="critical"),  # unmatched, blocking
        finding("x.py", 2, severity="low"),  # unmatched, not blocking
        finding("x.py", 3, severity="high", status="resolved"),  # not open
    ]
    assert unmatched_blocking(findings, DEFECTS) == 1


def test_qa_catch_counts_failing_criteria_reported_failed():
    results = [
        {"criterion_id": "REQ-1.1", "passed": False},
        {"criterion_id": "REQ-1.2", "passed": True},
        {"criterion_id": "REQ-2.1", "passed": False},
    ]
    assert qa_catch(results, ["REQ-1.1", "REQ-1.2", "REQ-3.1"]) == (1, 3)


REVIEW_DOC = """---
kind: review
schema_version: 1
run_id: r1
step: review
agent: code-review@1.0.0
produced_at: 2026-10-21T10:00:00Z
status: complete
summary: One issue.
findings:
  - {severity: high, file: billing/export.py, line: 42, issue: off by one}
  - {severity: high, file: other.py, line: 3, issue: unrelated}
open_blocking: 2
---
# Review

## Summary
s

## Findings
f

## Category breakdown
c

## Pre-pass results
p

## Verdict
v

## Open questions
None.
"""


def review_case() -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": "r1",
            "agent": "code-review",
            "case_class": "review",
            "split": "holdout",
            "source": SOURCE,
            "inputs": {"prompt": "review"},
            "expected": {"seeded_defects": [d.model_dump() for d in DEFECTS]},
        }
    )


def test_schema_valid_needs_a_completed_parsable_document_of_the_kind():
    assert schema_valid("completed", REVIEW_DOC, "review")
    assert not schema_valid("completed", REVIEW_DOC, "design")
    assert not schema_valid("invalid", REVIEW_DOC, "review")
    assert not schema_valid("completed", "not a document", "review")


def test_objective_metrics_for_a_review_run():
    record = RunRecord(status="completed", output=REVIEW_DOC, cost_usd=0.01, tokens_in=10)
    metrics = objective_metrics(review_case(), record, "review")
    assert metrics["schema_valid"] == 1.0
    assert metrics["defects_caught"] == 1 and metrics["defects_total"] == 2
    assert metrics["defect_recall"] == 0.5 and metrics["unmatched_blocking"] == 1
    assert metrics["cost_usd"] == 0.01


def test_the_judge_never_sees_the_envelope():
    from agentic.eval.metrics import judge_view

    shown = judge_view(REVIEW_DOC)
    assert "code-review@1.0.0" not in shown and "produced_at" not in shown
    assert "run_id" not in shown and "summary: One issue." in shown and "## Verdict" in shown
    assert judge_view("no front matter") == "no front matter"


#: ── DF-258: degenerate output cannot inflate the objective metrics ─────────────


def test_info_and_closed_findings_do_not_count_for_recall():
    from agentic.eval.metrics import defect_recall

    at_the_defect = [
        finding("billing/export.py", 41, severity="info"),
        finding("api/routes.py", 10, status="resolved"),
        finding("api/routes.py", 10, status="accepted"),
    ]
    assert defect_recall(at_the_defect, DEFECTS) == (0, 2)
    assert defect_recall([finding("api/routes.py", 10, severity="low")], DEFECTS) == (1, 2)


def test_diff_prefixes_match_the_seeded_path():
    result = match_findings(
        [finding("b/billing/export.py", 41), finding("a/api/routes.py", 10)], DEFECTS
    )
    assert result.caught == {0, 1}


def test_the_line_tolerance_shrinks_for_files_with_few_lines_shown():
    from agentic.eval.metrics import file_tolerances

    def diff(n: int) -> str:
        body = "".join(f"+x{i}\n" for i in range(n))
        return f"--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,{n} @@\n{body}"

    assert file_tolerances(diff(2)) == {"a.py": 0}
    assert file_tolerances(diff(8)) == {"a.py": 2}
    assert file_tolerances(diff(40)) == {"a.py": 3}
    assert file_tolerances(None) == {}
    near = [SeededDefect(file="a.py", lines=(2, 2), category="logic")]
    assert match_findings([finding("a.py", 4)], near).caught == {0}  # no diff: +/-3
    assert match_findings([finding("a.py", 4)], near, tolerances={"a.py": 1}).caught == set()


QA_REQUIREMENTS = """---
kind: requirements
summary: s
size: small
items:
  - id: REQ-1
    statement: s
    acceptance_criteria: [a, b]
  - id: REQ-2
    statement: s
    acceptance_criteria: [c]
---
# Requirements
"""


def qa_report(failed: list[str]) -> str:
    rows = "\n".join(
        f"  - {{criterion_id: {c}, passed: {str(c not in failed).lower()}, evidence: e}}"
        for c in ("REQ-1.1", "REQ-1.2", "REQ-2.1")
    )
    return (
        "---\nkind: test-report\nschema_version: 1\nrun_id: r\nstep: s\nagent: qa@1\n"
        "produced_at: 2026-10-21T10:00:00Z\nstatus: complete\nsummary: s\nverdict: fail\n"
        f"tests: {{passed: 1, failed: 0}}\nacceptance_results:\n{rows}\n---\n# Test report\n\n"
        "## Summary\ns\n\n## Verdict\nv\n\n## Test results\nt\n\n## Acceptance criteria\na\n\n"
        "## Dependency scan\nd\n\n## Open questions\nNone.\n"
    )


def qa_case() -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": "q1",
            "agent": "qa",
            "case_class": "qa",
            "split": "holdout",
            "source": SOURCE,
            "inputs": {"prompt": "check", "handoffs": {"requirements": QA_REQUIREMENTS}},
            "expected": {"failing_criteria": ["REQ-1.2"]},
        }
    )


def test_failing_every_criterion_shows_as_false_alarms():
    everything = RunRecord(status="completed", output=qa_report(["REQ-1.1", "REQ-1.2", "REQ-2.1"]))
    metrics = objective_metrics(qa_case(), everything, "test-report")
    assert metrics["qa_catch"] == 1.0
    assert metrics["criteria_false_alarms"] == 2 and metrics["criteria_passing"] == 2
    assert metrics["qa_false_alarm"] == 1.0
    exact = objective_metrics(
        qa_case(), RunRecord(status="completed", output=qa_report(["REQ-1.2"])), "test-report"
    )
    assert exact["qa_catch"] == 1.0 and exact["qa_false_alarm"] == 0.0


def test_false_alarms_are_a_pooled_rate_in_the_report():
    from agentic.eval.report import RATIO_METRICS

    assert RATIO_METRICS["qa_false_alarm"] == ("criteria_false_alarms", "criteria_passing")


#: ── DF-244: false findings ────────────────────────────────────────────────────


class FakeFindingJudge:
    def __init__(self, real: list[bool] | None):
        self.real, self.calls = real, []

    async def adjudicate(self, diff, findings):
        from agentic.eval.judge import FindingVerdict

        self.calls.append(findings)
        if self.real is None:
            return None
        return [FindingVerdict(real=r, reason="r") for r in self.real]


def evaluate_false_findings(judge, output=REVIEW_DOC):
    import asyncio

    from pydantic_evals.evaluators import EvaluatorContext

    from agentic.eval.metrics import FalseFindingMetric
    from agentic.eval.records import RunRequest

    record = RunRecord(status="completed", output=output)
    ctx = EvaluatorContext(
        name="r1.X.0",
        inputs=RunRequest(review_case(), "X", 0, "review", "r1.X.0"),
        metadata=None,
        expected_output=None,
        output=record,
        duration=0.0,
        _span_tree=None,
        attributes={},
        metrics={},
    )
    metric = FalseFindingMetric(judge_for=lambda request: judge)
    return asyncio.run(metric.evaluate(ctx)), record


def test_unmatched_findings_judged_not_real_are_false_findings():
    judge = FakeFindingJudge([False])
    metrics, record = evaluate_false_findings(judge)
    # REVIEW_DOC: one finding matches a seeded defect, one ('other.py') matches none
    assert [f["file"] for f in judge.calls[0]] == ["other.py"]
    assert metrics == {"false_findings": 1.0, "findings_total": 2.0, "false_finding_rate": 0.5}
    assert record.adjudications == [
        {"file": "other.py", "line": 3, "issue": "unrelated", "real": False, "reason": "r"}
    ]


def test_a_real_unmatched_issue_is_not_a_false_finding():
    metrics, _ = evaluate_false_findings(FakeFindingJudge([True]))
    assert metrics["false_findings"] == 0.0 and metrics["false_finding_rate"] == 0.0


def test_an_undecided_judge_leaves_false_findings_unmeasured():
    metrics, _ = evaluate_false_findings(FakeFindingJudge(None))
    assert metrics == {}


def test_false_finding_rate_is_a_pooled_rate_in_the_report():
    from agentic.eval.report import RATIO_METRICS

    assert RATIO_METRICS["false_finding_rate"] == ("false_findings", "findings_total")


def test_an_invalid_output_scores_zero_and_an_errored_run_is_unmeasured():
    invalid = objective_metrics(review_case(), RunRecord(status="invalid", output="x"), "review")
    assert invalid["schema_valid"] == 0.0 and invalid["defects_caught"] == 0
    errored = objective_metrics(review_case(), RunRecord(status="error", error="boom"), "review")
    assert errored == {}  # unmeasured, never zero
    assert "cost_usd" not in objective_metrics(
        review_case(), RunRecord(status="completed", output=REVIEW_DOC), "review"
    )
