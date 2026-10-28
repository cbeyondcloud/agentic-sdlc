"""The CLI runs what the team gets (M2-23, M2-15): the team's overlay for this repository
applied to the pipeline, each definition at the version the rollout or the team's pin
gives it, and a deprecated version warned before its date and refused from it. The plan
and `pipelines list` show which overlay applied. The policy here is a local policy repo
(`AGENTIC_POLICY_REPO`), the standalone loader's path; org mode is in test_org_preflight."""

import shutil
from datetime import date

import pytest
from _cli_support import git, write

from agentic_cli import workspace as workspace_module
from agentic_cli.exit_codes import ExitCode

OVERLAY = "teams/payments/pipelines/overlays/app.yaml"


@pytest.fixture
def origin(workspace):
    git(workspace, "remote", "add", "origin", "https://github.com/acme/app.git")
    return workspace


@pytest.fixture
def overlaid(origin, policy_repo):
    write(
        policy_repo / OVERLAY,
        "pipelines:\n  deliver:\n    remove: [design]\n    max_minutes: 30\n",
    )
    return policy_repo


def dry_run(cli, *flags: str):
    return cli(*flags, "run", "deliver", "--input", "request=x", "--dry-run")


def test_dry_run_shows_the_overlay_and_its_changes(overlaid, cli):
    result = dry_run(cli, "--json")
    assert result.code == ExitCode.OK, result.out + result.err
    plan = result.json()
    assert [step["id"] for step in plan["steps"]] == ["requirements", "requirements-approval"]
    assert plan["max_minutes"] == 30
    assert plan["overlay"] == {
        "file": OVERLAY,
        "changes": ["removed design", "max_minutes None -> 30"],
    }
    human = dry_run(cli)
    assert f"overlay {OVERLAY}: removed design; max_minutes None -> 30" in human.out


def test_no_overlay_without_an_origin(workspace, policy_repo, cli):
    """A repository without an origin cannot be told apart from another: no overlay."""
    write(policy_repo / OVERLAY, "pipelines:\n  deliver:\n    remove: [design]\n")
    plan = dry_run(cli, "--json").json()
    assert plan["overlay"] is None and len(plan["steps"]) == 3


def test_pipelines_list_shows_overlay(overlaid, cli):
    rows = cli("--json", "pipelines", "list").json()
    (deliver,) = [row for row in rows if row["name"] == "deliver"]
    assert deliver["overlay"] == OVERLAY and deliver["steps"] == 2
    assert "app.yaml" in cli("pipelines", "list").out


def test_a_refused_overlay_names_its_rule(origin, policy_repo, cli):
    org = policy_repo / "policy" / "org.yaml"
    org.write_text(
        org.read_text(encoding="utf-8") + "required_steps:\n  deliver: [requirements-approval]\n",
        encoding="utf-8",
    )
    write(policy_repo / OVERLAY, "pipelines:\n  deliver:\n    remove: [requirements-approval]\n")
    result = dry_run(cli)
    assert result.code == ExitCode.INVALID
    assert "app.yaml" in result.err and "rule required-step" in result.err


@pytest.fixture
def rolled(workspace, policy_repo):
    """ba 1.0.0 is rolled out to team web only; payments keeps ba 0.9.0 (archived)."""
    write(policy_repo / "policy" / "teams" / "web.yaml", "allowed_tiers: [fast]\n")
    write(
        policy_repo / "agents" / "versions" / "ba" / "0.9.0.md",
        "---\nname: ba\ntype: analyst\nversion: 0.9.0\ndescription: The old analyst.\n---\n"
        "You are ba.\n",
    )
    write(
        policy_repo / "policy" / "rollout.yaml",
        "rollout:\n  agents:\n    ba: {version: 1.0.0, previous: 0.9.0, teams: [web]}\n"
        "deprecated:\n  agents:\n    architect:\n"
        "      '2.1.0': {blocked_from: 2026-12-01, note: use architect 3}\n",
    )
    return policy_repo


def test_team_outside_rollout_runs_previous_agent_version(rolled, cli, monkeypatch):
    monkeypatch.setattr(workspace_module, "utc_today", lambda: date(2026, 11, 1))
    plan = dry_run(cli, "--json").json()
    assert plan["steps"][0]["version"] == "0.9.0"
    assert "agent ba@0.9.0 (your team is not in the rollout of 1.0.0)" in plan["versions"]
    monkeypatch.setenv("AGENTIC_TEAM", "web")
    assert dry_run(cli, "--json").json()["steps"][0]["version"] == "1.0.0"


def test_standalone_policy_honours_rollout(rolled, cli, monkeypatch, tmp_path):
    """The policy `agentic init --standalone` installs under the agentic home is read by
    the same loader: the rollout and the deprecations apply there too."""
    installed = tmp_path / "home" / "policy" / "standalone"
    shutil.copytree(rolled, installed)
    (rolled / "policy" / "rollout.yaml").unlink()  # only the installed copy rolls out
    monkeypatch.delenv("AGENTIC_POLICY_REPO")
    monkeypatch.setattr(workspace_module, "utc_today", lambda: date(2026, 12, 1))
    result = dry_run(cli, "--json")
    plan = result.json()
    assert plan["steps"][0]["version"] == "0.9.0", plan
    assert "agent ba@0.9.0 (your team is not in the rollout of 1.0.0)" in plan["versions"]
    assert result.code == ExitCode.INVALID  # architect@2.1.0 is refused from 2026-12-01


def test_deprecated_agent_blocked_after_date_in_cli(rolled, cli, monkeypatch):
    monkeypatch.setattr(workspace_module, "utc_today", lambda: date(2026, 11, 30))
    before = dry_run(cli, "--json")
    assert before.code == ExitCode.OK, before.out
    (warning,) = [p for p in before.json()["preflight"]["problems"] if p["check"] == "version"]
    assert warning["severity"] == "warning" and "refused from 2026-12-01" in warning["detail"]
    monkeypatch.setattr(workspace_module, "utc_today", lambda: date(2026, 12, 1))
    after = dry_run(cli)
    assert after.code == ExitCode.INVALID
    assert "architect@2.1.0" in after.out and "refused since 2026-12-01" in after.out
    assert "use architect 3" in after.out
