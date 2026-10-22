"""Org-mode init with a control plane (SP15 Task 10): roles come from `/v1/me`, never from
`~/.agentic/config.yaml` (DF-203, Review Focus 4); `init --repo` asks the control plane for
`agent.edit.repo` (check 7, DF-184) and registers the repo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cli_helpers import git, snapshot
from cp_fakes import CP_URL, TOKEN, FakeControlPlane
from test_init_dev import deps, policy_repo, setup_repo  # noqa: F401 - shared fixtures

from agentic import auth
from agentic_cli import control_plane, dev_init
from agentic_cli.config import agentic_home
from agentic_cli.control_plane import CONTROL_PLANE_TOKEN, record_login
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main


@pytest.fixture
def plane(policy_repo: Path, home: Path, monkeypatch) -> FakeControlPlane:  # noqa: F811
    """The fixture policy (it allows github.com/acme/*) served by a fake control plane."""
    fake = FakeControlPlane(policy_root=policy_repo)
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    for name in ("AGENTIC_CONTROL_PLANE_URL", "AGENTIC_CONTROL_PLANE_TOKEN", "AGENTIC_TEAM"):
        monkeypatch.delenv(name, raising=False)
    for kind in ("AUTHOR", "COMMITTER"):  # init --repo commits on a branch
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Repo Owner")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "owner@example.invalid")
    config_home = agentic_home()
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        f"org: acme\ncontrol_plane_url: {CP_URL}\nteam: payments\nroles: [org-admin]\n",
        encoding="utf-8",
    )
    auth.Credentials().store(CONTROL_PLANE_TOKEN, TOKEN)
    record_login(config_home, CP_URL, "acme", "dev@acme.example")
    return fake


def test_org_init_takes_roles_from_control_plane(setup_repo, deps, plane, monkeypatch, capsys):  # noqa: F811
    seen = {}
    real = dev_init.run_dev_init

    def capture(root, loaded, home, deps, **kwargs):
        seen["roles"] = deps.roles
        return real(root, loaded, home, deps, **kwargs)

    monkeypatch.setattr(dev_init, "run_dev_init", capture)
    monkeypatch.chdir(setup_repo)
    main(["init"])
    assert seen["roles"] == ("developer",)  # from /v1/me, not the config's org-admin
    assert "roles in ~/.agentic/config.yaml are ignored" in capsys.readouterr().err


def test_org_init_roles_include_org_scope_and_own_team_only(
    setup_repo,  # noqa: F811
    deps,  # noqa: F811
    plane,
    monkeypatch,
):
    plane.assignments = [
        {"role": "auditor", "scope": "org"},
        {"role": "team-lead", "scope": "team", "team": "payments"},
        {"role": "developer", "scope": "team", "team": "billing"},
    ]
    seen = {}
    real = dev_init.run_dev_init

    def capture(root, loaded, home, deps, **kwargs):
        seen["roles"] = deps.roles
        return real(root, loaded, home, deps, **kwargs)

    monkeypatch.setattr(dev_init, "run_dev_init", capture)
    monkeypatch.chdir(setup_repo)
    main(["--json", "init"])
    assert seen["roles"] == ("auditor", "team-lead")


def test_org_init_repo_checks_agent_edit_repo(repo, plane, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert main(["--json", "init", "--repo", "--team", "payments", "--no-pr"]) == ExitCode.OK
    check = [r for r in plane.requests if r.url.path == "/v1/rbac/check"]
    assert json.loads(check[0].content) == {
        "permission": "agent.edit.repo",
        "team": "payments",
        "repo": "github.com/acme/app",
    }
    assert plane.registered == [{"remote": "https://github.com/acme/app.git", "team": "payments"}]
    out = json.loads(capsys.readouterr().out)
    assert out["registered"] == "github.com/acme/app"


@pytest.mark.parametrize(
    ("origin", "sent"),
    [
        ("ssh://git@github.com/acme/app.git", "ssh://git@github.com/acme/app.git"),
        ("git@github.com:acme/app.git", "git@github.com:acme/app.git"),
        (
            "https://x-token:" + "s3cr3t" + "@github.com/acme/app.git",
            "https://github.com/acme/app.git",
        ),
    ],
)
def test_org_init_repo_registers_a_shareable_remote(repo, plane, monkeypatch, origin, sent):
    """Review F3: an ssh user is part of the address and is kept; https credentials are not
    sent to the control plane."""
    git(repo, "remote", "set-url", "origin", origin)
    monkeypatch.chdir(repo)
    assert main(["--json", "init", "--repo", "--team", "payments", "--no-pr"]) == ExitCode.OK
    assert plane.registered[-1]["remote"] == sent


def test_org_init_repo_refused_without_agent_edit_repo(repo, plane, monkeypatch, capsys):
    plane.allowed = set()
    before = snapshot(repo)
    monkeypatch.chdir(repo)
    assert main(["--json", "init", "--repo", "--team", "payments", "--no-pr"]) == ExitCode.REFUSED
    error = json.loads(capsys.readouterr().err)
    assert "agent.edit.repo" in error["error"]
    assert snapshot(repo) == before
    assert plane.registered == []
