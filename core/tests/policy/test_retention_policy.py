"""The retention policy (M3-06, OR-01, owner decision 115): `retention.yaml` sets how long
each kind of data is kept and which legal holds stop deletion. The audit chain is kept
longest and never below its floor."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy
from agentic.policy.model import (
    AUDIT_FLOOR_DAYS,
    GATEWAY_USAGE_FLOOR_DAYS,
    LegalHold,
    RetentionDays,
)
from agentic.yamlio import load_yaml

REPO = Path(__file__).resolve().parents[3]


def with_retention(fixtures: Path, tmp_path: Path, text: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    (root / "retention.yaml").write_text(text, encoding="utf-8")
    return root


def test_defaults_are_decision_115():
    days = RetentionDays()
    assert days.model_dump() == {
        "audit": 2555,
        "runs": 400,
        "gateway_usage": 400,
        "traces": 30,
        "feedback": 365,
        "run_folders": 30,
        "logs": 30,
        "backups": 35,
    }
    assert AUDIT_FLOOR_DAYS == 2555 and GATEWAY_USAGE_FLOOR_DAYS == 400


def test_a_policy_without_retention_yaml_gets_the_defaults(fixtures: Path, tmp_path: Path):
    root = with_retention(fixtures, tmp_path, "")
    (root / "retention.yaml").unlink()
    policy = load_policy(root)
    assert policy.retention_days == RetentionDays()
    assert policy.legal_holds == []


def test_periods_and_holds_are_read(fixtures: Path, tmp_path: Path):
    root = with_retention(
        fixtures,
        tmp_path,
        "days: { audit: null, runs: 200, feedback: null }\n"
        "legal_holds:\n"
        "  - { id: LH-1, reason: 'Case 42', data: [runs, audit], teams: [payments] }\n"
        "  - { id: LH-2, reason: everything }\n",
    )
    policy = load_policy(root)
    assert policy.retention_days.audit is None
    assert policy.retention_days.runs == 200
    assert policy.retention_days.feedback is None  # kept forever
    assert policy.retention_days.logs == 30  # unnamed kinds keep their defaults
    assert policy.legal_holds == [
        LegalHold(id="LH-1", reason="Case 42", data=["runs", "audit"], teams=["payments"]),
        LegalHold(id="LH-2", reason="everything"),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("days: { audit: 365 }", "set audit: 2555 or more"),
        ("days: { audit: 400, runs: 500 }", "2555"),
        ("days: { audit: 3000, runs: 3100 }", "audit is kept longest"),
        ("days: { audit: 3000, runs: null }", "audit is kept longest"),
        ("days: { gateway_usage: 100 }", "400"),
        ("days: { runs: 0 }", "greater than or equal to 1"),
        ("days: { run_folders: null }", "run_folders"),
        ("days: { prompts: 30 }", "prompts"),
        ("legal_holds: [ { id: LH-1, reason: x, data: [mail] } ]", "legal_holds.0.data"),
        ("legal_holds: [ { id: LH-1, reason: '' } ]", "reason"),
        ("legal_holds: [ { id: LH-1, reason: x }, { id: LH-1, reason: y } ]", "LH-1"),
        ("legal_holds: [ { id: LH-1, reason: x, teams: [nobody] } ]", "nobody"),
    ],
)
def test_bad_periods_and_holds_are_refused(fixtures, tmp_path, text, message):
    root = with_retention(fixtures, tmp_path, text + "\n")
    with pytest.raises(PolicyError, match=message):
        load_policy(root)


def test_audit_may_be_kept_forever(fixtures: Path, tmp_path: Path):
    root = with_retention(fixtures, tmp_path, "days: { audit: null, runs: null }\n")
    policy = load_policy(root)
    assert policy.retention_days.audit is None and policy.retention_days.runs is None


def test_gc_follows_run_folders_unless_limits_yaml_sets_it(fixtures: Path, tmp_path: Path):
    root = with_retention(fixtures, tmp_path, "days: { run_folders: 9 }\n")
    assert load_policy(root).local_data.gc_after_days == 9
    limits = root / "limits.yaml"
    limits.write_text(
        limits.read_text(encoding="utf-8") + "\nlocal: { gc_after_days: 4 }\n", encoding="utf-8"
    )
    assert load_policy(root).local_data.gc_after_days == 4


@pytest.mark.parametrize("template", ["policy-repo", "standalone"])
def test_the_templates_carry_the_defaults(template: str):
    path = REPO / "templates" / template / "policy" / "retention.yaml"
    data, _ = load_yaml(path.read_text(encoding="utf-8"))
    assert RetentionDays(**data["days"]) == RetentionDays()
