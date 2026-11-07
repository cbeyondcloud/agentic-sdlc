"""scripts/live_caps.py (DF-108, owner decision 2026-11-07): the committed spend caps load,
the gate's per-agent split fits its total, and the amount still allowed counts every
earlier attempt (reruns included) and stops at the cap."""

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_caps import (  # noqa: E402
    CAPS,
    CapsError,
    eval_budgets,
    load_caps,
    main,
    remaining,
    scan,
    spent,
)


def write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def report(folder: Path, agent: str, cost: float, judge: float | None = None, mode="record"):
    data = {"mode": mode, "agent": agent, "cost_total_usd": cost, "judge_cost_usd": judge}
    return write(folder / "report.json", data)


def run(folder: Path, spent_usd: str, child_usd: str = "0") -> Path:
    child = {"name": "s-1", "scope": "step", "limit": "1", "spent": child_usd, "children": []}
    budget = {"name": "run", "scope": "run", "limit": "20", "spent": spent_usd}
    return write(folder / "run.json", {"budget": {**budget, "children": [child]}})


def test_the_committed_caps_are_the_owner_decision():
    caps = load_caps(CAPS)
    assert caps.credit_limit == Decimal("300")
    assert caps.total("acceptance-1") == Decimal("20")
    assert caps.total("m1-64-gate") == Decimal("250")
    split = caps.by_agent("m1-64-gate")
    assert sum(split.values()) == Decimal("250.00")
    assert split["developer"] == Decimal("144.23") and split["qa"] == Decimal("48.08")
    # in proportion to S10's placeholder A/B caps: 100, 100, 100, 250, 750
    for agent, placeholder in {"design": 100, "qa": 250, "developer": 750}.items():
        assert abs(split[agent] - Decimal(250 * placeholder) / 1300) < Decimal("0.01")


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (
            "version: 1\nprovider_key_credit_limit_usd: 10\ncampaigns: {a: {total_usd: 20}}\n",
            "credit limit",
        ),
        (
            "version: 1\nprovider_key_credit_limit_usd: 300\n"
            "campaigns: {g: {total_usd: 10, by_agent: {qa: 8, dev: 8}}}\n",
            "more than",
        ),
        (
            "version: 1\nprovider_key_credit_limit_usd: 300\ncampaigns: {a: {total_usd: 0}}\n",
            "above zero",
        ),
    ],
)
def test_caps_that_do_not_add_up_are_refused(tmp_path, text, why):
    path = tmp_path / "caps.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CapsError, match=why):
        load_caps(path)


def test_spend_counts_every_attempt_and_only_paid_modes(tmp_path):
    report(tmp_path / "qa" / "t1", "qa", 3.0, 0.5)
    report(tmp_path / "qa" / "t2", "qa", 1.25)  # a rerun
    report(tmp_path / "qa" / "t3", "qa", 9.0, mode="replay")  # cassettes: nothing billed
    report(tmp_path / "developer" / "t1", "developer", 10.0)
    assert spent([tmp_path]) == Decimal("14.75")
    assert spent([tmp_path], agent="qa") == Decimal("4.75")


def test_run_spend_is_read_from_each_run_state(tmp_path):
    run(tmp_path / "runs" / "r-1", "2.5", child_usd="0.5")  # an open step's spend counts
    run(tmp_path / "runs" / "r-2", "1")  # a rerun
    assert spent([tmp_path]) == Decimal("4.0")


def test_remaining_is_the_tighter_of_the_agent_cap_and_the_total(tmp_path):
    caps = load_caps(CAPS)
    report(tmp_path / "qa" / "t1", "qa", 40.0)
    assert remaining(caps, "m1-64-gate", [tmp_path], agent="qa") == Decimal("8.08")
    report(tmp_path / "developer" / "t1", "developer", 200.0)  # over its own cap already
    assert remaining(caps, "m1-64-gate", [tmp_path], agent="design") == Decimal("10.00")
    assert remaining(caps, "acceptance-1", [tmp_path / "none"]) == Decimal("20")


def test_main_prints_what_is_left_and_stops_at_the_cap(tmp_path, capsys):
    run(tmp_path / "runs" / "r-1", "12.5")
    assert main(["remaining", "acceptance-1", "--spent-in", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "7.50"

    run(tmp_path / "runs" / "r-2", "7.5")
    assert main(["remaining", "acceptance-1", "--spent-in", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cap reached" in captured.err and "20.00 of 20" in captured.err


def test_main_checks_the_caps_file(capsys):
    assert main(["check"]) == 0
    assert "300" in capsys.readouterr().out


def test_an_unknown_campaign_or_agent_is_a_usage_error(tmp_path, capsys):
    assert main(["remaining", "nope", "--spent-in", str(tmp_path)]) == 2
    assert main(["remaining", "m1-64-gate", "--agent", "nobody", "--spent-in", str(tmp_path)]) == 2


def test_the_gate_caps_become_the_eval_policy_budgets():
    from agentic.policy.model import EvalPolicy

    budgets = eval_budgets(load_caps(CAPS), "m1-64-gate")
    policy = EvalPolicy(repeats=3, **budgets)
    assert policy.budget_for("developer") == 144.23 and policy.budget_for("qa") == 48.08
    assert policy.budget_for("someone-else") == 19.23  # the smallest share, never more


# ── review fixes: never undercount silently ──────────────────────────────────


def test_a_missing_or_absent_spent_in_folder_is_refused(tmp_path, capsys):
    assert main(["remaining", "acceptance-1"]) == 2
    assert "--spent-in" in capsys.readouterr().err
    assert main(["remaining", "acceptance-1", "--spent-in", str(tmp_path / "gone")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_a_stopped_evaluation_counts_at_least_its_budget(tmp_path):
    data = {"mode": "record", "agent": "qa", "cost_total_usd": 3.0, "judge_cost_usd": None}
    write(tmp_path / "qa" / "t1" / "report.json", {**data, "budget_hit": True, "budget_usd": 10})
    assert spent([tmp_path], agent="qa") == Decimal("10")


def test_unmeasured_runs_and_crashed_attempts_are_warned_about(tmp_path):
    data = {"mode": "record", "agent": "qa", "cost_total_usd": 1.0, "cost_unmeasured_runs": 2}
    write(tmp_path / "qa" / "t1" / "report.json", data)
    write(tmp_path / "qa" / "t1" / "judged" / "case.json", {})  # inside a report: fine
    (tmp_path / "qa" / "t2").mkdir(parents=True)
    (tmp_path / "qa" / "t2" / "partial.log").write_text("crashed", encoding="utf-8")
    total, warnings = scan([tmp_path])
    assert total == Decimal("1.0")
    text = " ".join(warnings)
    assert "2 run(s) with no measured cost" in text
    assert "t2" in text and "no report.json" in text and "judged" not in text


def test_main_prints_warnings_on_stderr(tmp_path, capsys):
    (tmp_path / "t2").mkdir()
    (tmp_path / "t2" / "out.txt").write_text("x", encoding="utf-8")
    assert main(["remaining", "acceptance-1", "--spent-in", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "20.00" and "warning:" in captured.err
