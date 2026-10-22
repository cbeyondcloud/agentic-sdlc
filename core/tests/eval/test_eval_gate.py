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
FLOOR = 20  # decision 18: holdout cases per gated agent


def case_for(agent: str, case_id: str) -> EvalCase:
    if agent not in ("code-review", "qa"):
        return scoping_case(case_id)
    defect = {"file": "a.py", "lines": [1, 1], "category": "logic"}
    expected = {"seeded_defects": [defect]} if agent == "code-review" else {}
    if agent == "qa":
        expected = {"failing_criteria": ["REQ-1.1"]}
    return EvalCase.model_validate(
        {
            "id": case_id,
            "agent": agent,
            "case_class": "review" if agent == "code-review" else "qa",
            "split": "holdout",
            "source": {"repo": "synthetic", "synthetic": True, "licence": "CC0-1.0"},
            "inputs": {"prompt": agent},
            "expected": expected,
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
    return {f"c{i}": {"rubric": rubric, "schema_valid": 1.0} for i in range(FLOOR)}


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
    review = {f"c{i}": {**caught, "schema_valid": 1.0} for i in range(FLOOR)}
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
        case["cost"] = [0.1, 0.3, 0.2][i % 3]
    gate = evaluate_gate([report("business-analyst", per_case)], agents=["business-analyst"])
    assert gate.median_cost_usd == {"business-analyst": pytest.approx(0.2)}
    text = render_gate(gate)
    assert "business-analyst" in text and "rubric" in text and "PASS" in text


def qa(catch: float, false_alarm: float, cases: int = FLOOR) -> dict:
    metrics = {
        "criteria_caught": catch * 2,
        "criteria_total": 2.0,
        "criteria_false_alarms": false_alarm * 2,
        "criteria_passing": 2.0,
        "qa_catch": catch,
        "qa_false_alarm": false_alarm,
        "schema_valid": 1.0,
    }
    return {f"c{i}": dict(metrics) for i in range(cases)}


def test_a_qa_agent_that_fails_every_criterion_does_not_pass():
    """F3: marking every criterion failed catches every seeded one (qa_catch 1.0); the
    false-alarm target keeps that agent out."""
    assert DEFAULT_TARGETS["qa"][-1].metric == "qa_false_alarm"
    alarmist = evaluate_gate([report("qa", qa(1.0, 1.0))], agents=["qa"])
    rows = {r.metric: r for r in alarmist.rows}
    assert rows["qa_catch"].verdict == "PASS"
    assert rows["qa_false_alarm"].verdict == "FAIL"
    assert alarmist.verdict == "FAIL"
    assert evaluate_gate([report("qa", qa(1.0, 0.0))], agents=["qa"]).verdict == "PASS"


def test_a_report_below_the_decision_18_floor_is_not_a_gate():
    """F3: a live, MEASURED holdout report on a tiny corpus cannot pass the gate."""
    small = {f"c{i}": {"rubric": 9.0, "schema_valid": 1.0} for i in range(2)}
    gate = evaluate_gate([report("business-analyst", small)], agents=["business-analyst"])
    assert gate.verdict == "NOT_A_GATE"
    assert any("2 cases" in p and "at least 20" in p for p in gate.problems)
    few_qa = evaluate_gate([report("qa", qa(1.0, 0.0, cases=14))], agents=["qa"])
    assert few_qa.verdict == "NOT_A_GATE"


def test_a_metric_scored_on_too_few_cases_is_not_measured():
    """F3: enough cases ran but a target's metric scored fewer than the floor."""
    per_case = ba(8.0)
    for case in list(per_case.values())[:5]:
        del case["rubric"]
    gate = evaluate_gate([report("business-analyst", per_case)], agents=["business-analyst"])
    row = next(r for r in gate.rows if r.metric == "rubric")
    assert row.verdict == "NOT_MEASURED" and "15 of 20" in row.note
    assert gate.verdict == "INCOMPLETE"
