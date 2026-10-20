"""The eval report (M1-63): aggregates per case, pooled rates, unscored cases named,
verdict, JSON + Markdown, and the deterministic view CI compares."""

import json
from pathlib import Path

from _eval_support import scoping_case

from agentic.eval.records import RunRecord, RunRequest
from agentic.eval.report import build_report, comparable, render_markdown, write_report
from agentic.eval.runner import EvalRunResult, RunSet


def run(case_id: str, repeat: int, metrics: dict, status="completed", cost=0.1, label="X"):
    request = RunRequest(
        scoping_case(case_id), label, repeat, "requirements", f"{case_id}.{label}.{repeat}"
    )
    record = RunRecord(status=status, output=f"out {case_id} {repeat}", cost_usd=cost)
    record.model_ids = ["family-a/m1"]
    if status == "error":
        record.error = "boom"
    return EvalRunResult(request, record, metrics)


def runset(runs, budget_hit=False):
    return RunSet(runs, repeats=2, seed=4, budget_usd=10.0, budget_hit=budget_hit, spent_usd=0.4)


def report_of(runs, **kwargs):
    defaults = dict(
        report_id="r-1",
        created_at="2026-10-21T10:00:00Z",
        mode="replay",
        agent="business-analyst",
        agent_version="1.0.0",
        kind="requirements",
        corpus_manifest_sha="sha256:" + "a" * 64,
        split="holdout",
        labels={"X": "business-analyst@1.0.0"},
    )
    return build_report(runset(runs, kwargs.pop("budget_hit", False)), **{**defaults, **kwargs})


def test_aggregates_average_repeats_within_a_case_first():
    report = report_of(
        [
            run("c1", 0, {"rubric": 6.0, "schema_valid": 1.0}),
            run("c1", 1, {"rubric": 8.0, "schema_valid": 1.0}),
            run("c2", 0, {"rubric": 9.0, "schema_valid": 1.0}),
            run("c2", 1, {"schema_valid": 0.0}),  # rubric unmeasured in this run
        ]
    )
    rubric = report.aggregates["X"]["rubric"]
    assert rubric.value == (7.0 + 9.0) / 2 and rubric.median == 8.0
    assert rubric.n_scored == 2 and rubric.unscored == []
    assert report.aggregates["X"]["schema_valid"].value == (1.0 + 0.5) / 2
    assert report.verdict == "MEASURED"
    assert report.cases == ["c1", "c2"] and report.k == 2


def test_ratio_metrics_are_pooled_over_defects():
    report = report_of(
        [
            run("c1", 0, {"defects_caught": 2.0, "defects_total": 2.0, "defect_recall": 1.0}),
            run("c2", 0, {"defects_caught": 1.0, "defects_total": 4.0, "defect_recall": 0.25}),
        ]
    )
    recall = report.aggregates["X"]["defect_recall"]
    assert recall.value == 3 / 6  # pooled, not the mean of 1.0 and 0.25
    assert recall.ci_low <= recall.value <= recall.ci_high


def test_unscored_cases_are_named_and_an_errored_run_is_listed():
    report = report_of(
        [
            run("c1", 0, {"rubric": 7.0}),
            run("c2", 0, {}, status="error"),
        ]
    )
    assert report.aggregates["X"]["rubric"].unscored == ["c2"]
    assert report.unscored_runs == ["c2.X.0"]


def test_leaks_and_replay_problems_make_it_unmeasurable_and_the_budget_incomplete():
    runs = [run("c1", 0, {"rubric": 7.0})]
    assert report_of(runs, leaks=["with-skill"]).verdict == "UNMEASURABLE"
    assert report_of(runs, problems=["missing cassette c1/X/0"]).verdict == "UNMEASURABLE"
    assert report_of(runs, budget_hit=True).verdict == "INCOMPLETE"
    assert report_of([run("c1", 0, {}, status="error")]).verdict == "UNMEASURABLE"


def test_cost_and_model_changes_are_reported():
    runs = [run("c1", 0, {}), run("c2", 0, {}, cost=None)]
    runs[1].record.model_ids = ["family-a/m2"]
    report = report_of(runs)
    assert report.cost_total_usd == 0.1 and report.cost_unmeasured_runs == 1
    assert report.model_ids == ["family-a/m1", "family-a/m2"] and report.model_changed


def test_markdown_and_files(tmp_path: Path):
    runs = [run("c1", 0, {"rubric": 7.0, "schema_valid": 1.0})]
    runs[0].record.judged = "what the judge saw"
    report = report_of(runs, stale_cassettes=1)
    markdown = render_markdown(report)
    assert "business-analyst" in markdown and "rubric" in markdown and "MEASURED" in markdown
    assert "stale" in markdown
    paths = write_report(report, tmp_path / "out", runs)
    data = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert data["verdict"] == "MEASURED" and data["schema_version"] == 1
    assert b"\r\n" not in (tmp_path / "out" / "report.md").read_bytes()
    assert (tmp_path / "out" / "judged" / "c1.X.0.md").read_text(encoding="utf-8") == (
        "what the judge saw"
    )
    assert set(paths) == {"json", "markdown"}


def test_comparable_drops_ids_times_and_durations():
    runs = [run("c1", 0, {"rubric": 7.0, "duration_s": 1.5})]
    runs[0].record.duration_s = 1.5
    first = comparable(report_of(runs))
    runs[0].record.duration_s = 9.0
    runs[0].metrics["duration_s"] = 9.0
    second = comparable(report_of(runs, report_id="r-2", created_at="2027-01-01T00:00:00Z"))
    assert first == second
    assert "report_id" not in first and "duration_s" not in first["aggregates"]["X"]
