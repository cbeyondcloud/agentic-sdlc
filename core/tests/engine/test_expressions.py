"""Tests for pipeline conditions: CEL over handoff front-matter fields (M1-23, spike S13)."""

import re

import pytest

from agentic.engine.expressions import Condition, ExpressionError, cel_name, compile_condition

CTX = {
    "requirements": {"size": "small", "items": [{"id": "REQ-1"}]},
    "review": {"open_blocking": 0, "score": None, "findings": [{"severity": "low"}]},
}


def test_the_spec_expressions_evaluate():
    assert compile_condition("requirements.size != 'trivial'").evaluate(CTX) is True
    assert compile_condition("review.open_blocking == 0").evaluate(CTX) is True
    assert compile_condition("review.open_blocking > 0").evaluate(CTX) is False


def test_a_condition_keeps_its_text():
    condition = compile_condition("review.open_blocking == 0")

    assert isinstance(condition, Condition) and condition.text == "review.open_blocking == 0"


def test_the_kinds_an_expression_reads_are_known_before_running():
    condition = compile_condition("review.open_blocking == 0 && has(requirements.size)")

    assert condition.kinds == frozenset({"review", "requirements"})


def test_lists_and_membership_work():
    assert compile_condition("size(review.findings) == 1").evaluate(CTX) is True
    assert compile_condition("requirements.size in ['small', 'medium']").evaluate(CTX) is True


def test_optional_fields_are_tested_with_has():
    assert compile_condition("has(review.score) && review.score == null").evaluate(CTX) is True
    assert compile_condition("has(review.missing)").evaluate(CTX) is False


@pytest.mark.parametrize("text", ["review.open_blocking ==", "9 ** 9", "review.(x)", ""])
def test_bad_syntax_fails_at_compile_time(text):
    with pytest.raises(ExpressionError, match=re.escape(text) if text else "empty"):
        compile_condition(text)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("design.decisions == 0", "design"),  # a kind no step produced
        ("review.missing == 0", "missing"),  # a field the handoff lacks
        ("review.open_blocking", "boolean"),  # not a condition
        ("review.__class__ == 1", "__class__"),
        ("'a' * 3 == 'aaa'", "'a' \\* 3"),  # no implicit string maths
    ],
)
def test_evaluation_problems_name_the_cause(text, reason):
    with pytest.raises(ExpressionError, match=reason):
        compile_condition(text).evaluate(CTX)


def test_the_error_names_the_expression():
    with pytest.raises(ExpressionError) as caught:
        compile_condition("review.missing == 0").evaluate(CTX)

    assert caught.value.expression == "review.missing == 0"
    assert str(caught.value).startswith("review.missing == 0: ")


def test_kinds_with_dashes_are_read_with_underscores():
    context = {**CTX, "dev-report": {"commits": ["3f2a9c1"]}, "test-report": {"verdict": "pass"}}

    assert compile_condition("size(dev_report.commits) == 1").evaluate(context) is True
    assert compile_condition("test_report.verdict == 'pass'").evaluate(context) is True
    assert compile_condition("dev_report.commits == []").kinds == frozenset({"dev_report"})
    assert cel_name("dev-report") == "dev_report"


def test_macro_variables_and_type_names_are_not_kinds():  # SP6 review I8
    assert compile_condition("review.findings.exists(f, f.status == 'open')").kinds == {"review"}
    assert compile_condition("review.findings.all(x, x.severity != 'critical')").kinds == {"review"}
    assert compile_condition("type(review.score) == double").kinds == {"review"}
