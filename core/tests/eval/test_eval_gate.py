"""The agent quality gate (M1-64 targets, DF-243): reports compared with policy targets."""

import pytest
from _eval_support import scoping_case

from agentic.eval.cases import EvalCase
from agentic.eval.gate import DEFAULT_TARGETS, evaluate_gate, render_gate
from agentic.eval.records import RunRecord, RunRequest
from agentic.eval.report import build_report
from agentic.eval.runner import EvalRunResult, RunSet
from agentic.policy import EvalPolicy

SHA = "sha256:" + "a" * 64


def case_for(agent: str, case_id: str) -> EvalCase:
    if agent != "code-review":
        return scoping_case(case_id)
    defect = {"file": "a.py", "lines": [1, 1], "category": "logic"}
    return EvalCase.model_validate(
        {
            "id": case_id,
            "agent": agent,
            "case_class": "review",
            "split": "holdout",
            "source": {"repo": "synthetic", "synthetic": True, "licence": "CC0-1.0"},
            "inputs": {"prompt": "review"},
            "expected": {"seeded_defects": [defect]},
        }
    )


def report(agent: str, per_case: dict[str, dict], **overrides):
    runs = []
    for case_id, metrics in per_case.items():
        request = RunRequest(case_for(agent, case_id), "X", 0, "requirements", f"{case_id}.X.0")
        record = RunRecord(status="completed", output="x", cost_usd=metrics.pop("cost", 0.1))
        runs.append(EvalRunResult(request, record, metrics))
    fields = dict(
        report_id=f"eval-{agent}",
        created_at="2026-10-21T10:00:00Z",
        mode="live",
        agent=agent,
        agent_version="1.0.0",
        kind="requirements",
        corpus_manifest_sha=SHA,
        split="holdout",
        labels={"X": f"{agent}@1.0.0"},
    )
    budget_hit = overrides.pop("budget_hit", False)
    runset = RunSet(runs, repeats=1, seed=0, budget_usd=10, budget_hit=budget_hit, spent_usd=0)
    return build_report(runset, **{**fields, **overrides})


def ba(rubric: float) -> dict:
    return {f"c{i}": {"rubric": rubric, "schema_valid": 1.0} for i in range(3)}


def test_default_targets_are_the_m1_64_initial_targets():
    by_agent = {a: {t.metric: t for t in ts} for a, ts in DEFAULT_TARGETS.items()}
    assert by_agent["business-analyst"]["rubric"].at_least == 7.0
    assert by_agent["business-analyst"]["rubric"].statistic == "median"
    assert by_agent["developer"]["tests_pass"].at_least == 0.5
    assert by_agent["code-review"]["defect_recall"].at_least == 0.7
    assert by_agent["code-review"]["false_finding_rate"].at_most == 0.3
    assert by_agent["qa"]["qa_catch"].at_least == 0.8
    assert by_agent["*"]["schema_valid"].at_least == 1.0


def test_a_report_meeting_its_targets_passes_and_one_missing_them_fails():
    good = evaluate_gate([report("business-analyst", ba(8.0))], agents=["business-analyst"])
    assert good.verdict == "PASS", good.problems
    assert {(r.metric, r.verdict) for r in good.rows} == {
        ("rubric", "PASS"),
        ("schema_valid", "PASS"),
    }
    bad = evaluate_gate([report("business-analyst", ba(6.0))], agents=["business-analyst"])
    assert bad.verdict == "FAIL"
    assert next(r for r in bad.rows if r.metric == "rubric").value == 6.0


def test_an_unmeasured_target_leaves_the_gate_incomplete():
    caught = {"defects_caught": 2.0, "defects_total": 2.0, "defect_recall": 1.0}
    review = {f"c{i}": {**caught, "schema_valid": 1.0} for i in range(3)}
    gate = evaluate_gate([report("code-review", review)], agents=["code-review"])
    rows = {r.metric: r for r in gate.rows}
    assert rows["defect_recall"].verdict == "PASS"
    assert rows["false_finding_rate"].verdict == "NOT_MEASURED"
    assert gate.verdict == "INCOMPLETE"


def test_a_missing_agent_report_leaves_the_gate_incomplete():
    gate = evaluate_gate([report("business-analyst", ba(8.0))], agents=["business-analyst", "qa"])
    assert gate.verdict == "INCOMPLETE"
    assert any(r.agent == "qa" and r.verdict == "NOT_MEASURED" for r in gate.rows)


@pytest.mark.parametrize(
    "overrides, problem",
    [
        ({"mode": "replay"}, "live"),
        ({"split": "dev"}, "holdout"),
        ({"tag": "fixture"}, "subset"),
        ({"budget_hit": True}, "INCOMPLETE"),
    ],
)
def test_reports_that_cannot_decide_a_gate_are_named(overrides, problem):
    gate = evaluate_gate(
        [report("business-analyst", ba(8.0), **overrides)], agents=["business-analyst"]
    )
    assert gate.verdict == "NOT_A_GATE"
    assert any(problem in p for p in gate.problems)


def test_reports_from_different_corpora_or_twice_one_agent_are_refused():
    other = report("design", ba(8.0), corpus_manifest_sha="sha256:" + "b" * 64)
    gate = evaluate_gate([report("business-analyst", ba(8.0)), other])
    assert gate.verdict == "NOT_A_GATE" and any("corpus" in p for p in gate.problems)
    twice = evaluate_gate(
        [report("business-analyst", ba(8.0)), report("business-analyst", ba(8.0))]
    )
    assert any("two reports" in p for p in twice.problems)


def test_policy_targets_replace_the_defaults_for_their_agent():
    policy = EvalPolicy.model_validate(
        {"targets": {"business-analyst": [{"metric": "rubric", "at_least": 9.0}]}}
    )
    gate = evaluate_gate(
        [report("business-analyst", ba(8.0))], targets=policy.targets, agents=["business-analyst"]
    )
    assert gate.verdict == "FAIL"
    assert [r.metric for r in gate.rows] == ["rubric", "schema_valid"]


def test_a_target_is_at_least_or_at_most():
    with pytest.raises(ValueError, match="exactly one"):
        EvalPolicy.model_validate({"targets": {"qa": [{"metric": "qa_catch"}]}})


def test_the_gate_records_the_median_cost_baseline_and_renders():
    per_case = ba(8.0)
    for i, case in enumerate(per_case.values()):
        case["cost"] = [0.1, 0.3, 0.2][i]
    gate = evaluate_gate([report("business-analyst", per_case)], agents=["business-analyst"])
    assert gate.median_cost_usd == {"business-analyst": pytest.approx(0.2)}
    text = render_gate(gate)
    assert "business-analyst" in text and "rubric" in text and "PASS" in text
