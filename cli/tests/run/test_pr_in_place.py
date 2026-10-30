"""Publishing a run on an in-place component (M2-61; DF-287): its work is pushed by its own
push command (the commit-command override), after the push wall, and its pull request is
opened as for a tree. Recorded fixtures only: the fake GitHub server and a local bare
remote."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from _cli_support import FakeGateway, answer, git, write
from fake_github import FakeGitHub

from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode

ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "commons_in_place_support_pr", ROOT / "commons" / "tests" / "in_place_support.py"
)
support = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(support)

KEY = "tl-" + "fake-gateway-key-0061"  # gitleaks:allow (not a real key)
TOKEN = "ghp_" + "InPlace" + "Fake0123456789"  # gitleaks:allow - built from pieces
HOST = "github.example.test"
SHIP = """name: ship
version: 1.0.0
regime: team
inputs:
  request: { from: [text] }
steps:
  - id: requirements
    agent: ba
  - id: publish
    action: open_pr
"""


def allow_override(policy_repo: Path) -> None:
    org = policy_repo / "policy" / "org.yaml"
    rules = ", ".join("'" + rule.replace("'", "''") + "'" for rule in support.ALLOW)
    org.write_text(
        org.read_text(encoding="utf-8") + f"commit_override: {{ allow: [{rules}] }}\n",
        encoding="utf-8",
    )


def make_in_place(workspace: Path, *, push: str = support.PUSH) -> None:
    """The workspace becomes a repository whose raw git is unsafe, with its own tool."""
    write(workspace / ".sdlc" / "profile.yml", support.profile(push=push))
    write(workspace / "tools" / "vcs.py", support.TOOL)
    rollback = support.ROLLBACK.replace("'", "''")
    write(workspace / ".agentic" / "config.yaml", f"rollback_commands: {{'.': '{rollback}'}}\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "in place")
    git(workspace, "switch", "-q", "-c", "work")  # a run never commits on the default branch


@pytest.fixture
def bare(tmp_path: Path, workspace: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(workspace, "remote", "add", "origin", f"https://{HOST}/acme/app.git")
    git(workspace, "remote", "set-url", "--push", "origin", str(remote))
    git(workspace, "push", "-q", "origin", "main")
    return remote


@pytest.fixture
def github(tmp_path, workspace, policy_repo, bare, monkeypatch):
    monkeypatch.setenv("VCS_TOOL_LOG", str(tmp_path / "tool.log"))
    fake = FakeGitHub(TOKEN, bare).start()
    write(
        tmp_path / "home" / "config.yaml",
        f"git_hosts:\n  {HOST}:\n    kind: github\n    api_url: {fake.url}\n",
    )
    write(policy_repo / "pipelines" / "ship.yaml", SHIP)
    allow_override(policy_repo)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    gateway = FakeGateway({"ba": [answer("requirements")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", gateway.transport)
    yield fake
    fake.stop()


def ship(cli):
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    return result, result.json().get("run_id")


def test_an_in_place_run_is_published_through_its_push_command(
    workspace, github, bare, cli, tmp_path
):
    make_in_place(workspace)
    git(workspace, "push", "-q", "origin", "main")
    result, run_id = ship(cli)
    assert result.code == ExitCode.OK, result.err
    assert result.json()["status"] == "completed"

    branch = f"sdlc/{run_id}"
    assert git(bare, "rev-parse", f"refs/heads/{branch}") == git(workspace, "rev-parse", "HEAD")
    [pull] = github.pulls
    assert (pull["head"]["ref"], pull["base"]["ref"]) == (branch, "main")
    assert (tmp_path / "tool.log").read_text(encoding="utf-8").split() == ["push"]

    folder = workspace / ".agentic" / "local" / "runs" / run_id
    [entry] = json.loads((folder / "pr.json").read_text("utf-8"))["pull_requests"]
    assert entry["branch"] == branch and entry["number"] == 1
    rows = (folder / "vcs-commands.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["action"] for row in rows] == ["push"]


def test_without_a_push_command_the_run_is_refused_before_it_spends(workspace, github, cli):
    make_in_place(workspace, push="")
    git(workspace, "push", "-q", "origin", "main")
    result, _ = ship(cli)
    assert result.code != ExitCode.OK and "nothing was started" in result.err
    assert "vcs.push_command" in result.out
    assert github.pulls == []


def test_without_org_allow_the_run_stops_before_any_agent(workspace, github, policy_repo, cli):
    org = policy_repo / "policy" / "org.yaml"
    org.write_text("allow_repo_pipelines: true\nsandbox: { required_for: [] }\n", "utf-8")
    make_in_place(workspace)
    result, _ = ship(cli)
    assert result.code != ExitCode.OK
    assert "commit_override.allow" in result.out + result.err
    assert github.pulls == []
