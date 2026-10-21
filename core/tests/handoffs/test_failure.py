"""Tests for `failure()`: which handoffs report a failure a run may not complete on
(DF-238): a review with open blocking findings, a QA report with a `fail` verdict."""

from pathlib import Path

import pytest

from agentic.handoffs.extensions import extend_catalog, load_extension
from agentic.handoffs.kinds import ReviewData, TestReportData
from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.yamlio import load_yaml, split_front_matter

COUNTS = {"passed": 41, "failed": 1, "skipped": 0}


def test_a_clean_review_and_a_passing_report_report_no_failure():
    assert ReviewData(summary="ok", open_blocking=0).failure() is None
    report = TestReportData(summary="ok", verdict="pass", tests={"passed": 3, "failed": 0})
    assert report.failure() is None


def test_a_review_with_open_blocking_findings_reports_them():
    findings = [{"severity": "high", "issue": "No timeout.", "status": "open"}]
    review = ReviewData(summary="blocked", findings=findings, open_blocking=1)
    assert review.failure() == "1 blocking finding open"
    assert ReviewData(summary="b", open_blocking=2).failure() == "2 blocking findings open"


def test_a_failing_report_names_its_new_failures_and_failed_criteria():
    report = TestReportData(
        summary="red",
        verdict="fail",
        tests=COUNTS,
        failures=["tests/test_a.py::test_new", "tests/test_b.py::test_old"],
        baseline_failures=["tests/test_b.py::test_old"],
        acceptance_results=[
            {"criterion_id": "REQ-1", "passed": False},
            {"criterion_id": "REQ-2", "passed": True},
        ],
    )
    reason = report.failure()
    assert reason.startswith("verdict 'fail'")
    assert "tests/test_a.py::test_new" in reason and "test_old" not in reason
    assert "REQ-1" in reason and "REQ-2" not in reason


def test_a_bare_failing_verdict_still_reports_a_failure():
    report = TestReportData(summary="red", verdict="fail", tests=COUNTS)
    assert report.failure() == "verdict 'fail'"


@pytest.mark.parametrize("kind", ["requirements", "design", "dev-report"])
def test_other_kinds_never_report_a_failure(kind):
    header, _ = split_front_matter(render_template(BUILTIN_CATALOG.schema(kind)))
    values, _ = load_yaml(header)
    values.pop("status", None)  # an envelope field the template shows
    assert BUILTIN_CATALOG.schema(kind).model.model_validate(values).failure() is None


def test_an_extended_kind_keeps_the_rule(tmp_path: Path):
    path = tmp_path / "handoffs" / "test-report.yaml"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b"fields:\n  ticket:\n    type: string\n    required: false\n"
        b"    description: The ticket\n    example: PAY-1\n"
    )
    catalog = extend_catalog(BUILTIN_CATALOG, [load_extension(path, "team")])
    model = catalog.schema("test-report").model

    report = model(summary="red", verdict="fail", tests=COUNTS)
    assert report.failure() == "verdict 'fail'"
