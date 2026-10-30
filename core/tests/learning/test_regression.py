"""The regression eval case every learning carries (M3-14), and its run before and after the
change through the eval harness (M3-16, DF-247) - scripted runners only, no model."""

import asyncio
from datetime import UTC, datetime

from agentic.eval.cases import EvalCase
from agentic.eval.records import RunRecord, RunRequest
from agentic.learning.changes import Evidence
from agentic.learning.regression import (
    case_path,
    lesson_coverage,
    measure,
    regression_case,
)

PID = "lp-" + "3" * 20
LESSON = "Name every CSV column in the acceptance criteria."
EVIDENCE = [
    Evidence(
        feedback_id="fb-1",
        signal="gate-rejection",
        actor="alice",
        given_at=datetime(2026, 10, 28, tzinfo=UTC),
        text="The criteria must name each CSV column.",
        run_id="run-1",
    )
]


def scripted(output: str):
    async def run(request: RunRequest) -> RunRecord:
        return RunRecord(status="completed", output=output)

    return run


def test_regression_case_is_a_valid_eval_case():
    case = regression_case(PID, "business-analyst", LESSON, EVIDENCE)
    assert isinstance(case, EvalCase)
    assert (case.id, case.agent, case.case_class, case.split) == (
        f"learned-{PID}",
        "business-analyst",
        "scoping",
        "dev",
    )
    assert case.expected.key_points == [LESSON] and case.rubric == "ba-requirements"
    assert "The criteria must name each CSV column." in case.inputs.prompt
    assert {"learning", "regression"} <= set(case.tags)
    assert regression_case(PID, "design", LESSON, EVIDENCE).rubric == "design"
    assert case_path("repo", PID) == f".agentic/eval/cases/scoping/learned-{PID}.yaml"
    assert case_path("home", PID) == f"eval/cases/scoping/learned-{PID}.yaml"
    assert case_path("policy", PID) == f"eval/cases/scoping/learned-{PID}.yaml"


def test_measure_runs_the_case_before_and_after():
    case = regression_case(PID, "business-analyst", LESSON, EVIDENCE)
    found = asyncio.run(
        measure(
            case,
            before=scripted("Export the invoices as CSV."),
            after=scripted("AC-1: the CSV names every column: id, date, amount."),
            kind="requirements",
        )
    )
    assert found.before < found.after and not found.regressed
    assert found.after == lesson_coverage([LESSON], "every CSV column named in the criteria")
    assert found.as_dict()["case"] == case.id


def test_lower_after_score_is_a_regression():
    case = regression_case(PID, "business-analyst", LESSON, EVIDENCE)
    found = asyncio.run(
        measure(
            case,
            before=scripted("Every CSV column is named in the acceptance criteria."),
            after=scripted("Export it."),
            kind="requirements",
        )
    )
    assert found.regressed
    failed = asyncio.run(
        measure(case, before=scripted("x"), after=scripted("x"), kind="requirements")
    )
    assert failed.before == failed.after == 0.0 and not failed.regressed
