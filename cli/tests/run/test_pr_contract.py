"""GitHub adapter contract (M1-58): a run's branch is pushed to a local bare remote, its pull
request is opened on a local fake GitHub API with the run and handoff links, and CI is read
and watched - never the real GitHub, never a merge, and the token is never shown or stored."""

import json
import logging
import subprocess
from pathlib import Path

import pytest
from _cli_support import FakeGateway, answer, git, write
from fake_github import FakeGitHub

from agentic_cli import publishing, runtime
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0058"  # gitleaks:allow (not a real key)
TOKEN = "ghp_" + "Contract" + "Fake0123456789"  # gitleaks:allow - built from pieces
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
    action: github.open_pr
"""


def success(name: str) -> dict:
    return {"name": name, "status": "completed", "conclusion": "success", "html_url": ""}


@pytest.fixture
def bare(tmp_path: Path, workspace: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(workspace, "remote", "add", "origin", f"https://{HOST}/acme/app.git")
    git(workspace, "remote", "set-url", "--push", "origin", str(remote))  # pushes go here
    git(workspace, "push", "-q", "origin", "main")
    return remote


@pytest.fixture
def github(tmp_path, workspace, policy_repo, bare, monkeypatch):
    fake = FakeGitHub(TOKEN, bare).start()
    write(
        tmp_path / "home" / "config.yaml",
        f"git_hosts:\n  {HOST}:\n    kind: github\n    api_url: {fake.url}\n",
    )
    write(policy_repo / "pipelines" / "ship.yaml", SHIP)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    gateway = FakeGateway({"ba": [answer("requirements")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", gateway.transport)
    yield fake
    fake.stop()


def ship(cli):
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    return result, result.json()["run_id"]


def run_folder(workspace: Path, run_id: str) -> Path:
    return workspace / ".agentic" / "local" / "runs" / run_id


def test_a_run_ends_by_pushing_its_branch_and_opening_the_pull_request(
    workspace, github, bare, cli
):
    result, run_id = ship(cli)
    assert result.code == ExitCode.OK, result.err
    assert result.json()["status"] == "completed"

    branch = f"sdlc/{run_id}"
    assert git(bare, "rev-parse", f"refs/heads/{branch}")  # really pushed
    [pull] = github.pulls
    assert (pull["head"]["ref"], pull["base"]["ref"]) == (branch, "main")
    assert "CSV export" in pull["title"]
    body = pull["body"]
    assert f"## Agentic run `{run_id}`" in body
    assert f".agentic/local/runs/{run_id}/handoffs/requirements-1.md" in body
    assert "sha256:" in body and "Started by: dev@example.invalid" in body
    assert "- Cost: $0.0125\n" in body  # the BA's one call, as the gateway reported it
    assert f"- Logs: `agentic logs {run_id}` in the starter's checkout\n" in body

    recorded = json.loads((run_folder(workspace, run_id) / "pr.json").read_text("utf-8"))
    [entry] = recorded["pull_requests"]
    assert entry["url"] == pull["html_url"] and entry["number"] == 1
    assert entry["head_sha"] == git(bare, "rev-parse", f"refs/heads/{branch}")
    assert not [p for p in github.paths() if "merge" in p]


def test_publishing_again_updates_the_same_pull_request(workspace, github, cli):
    _, run_id = ship(cli)
    again = cli("--json", "pr", "open", run_id)
    assert again.code == ExitCode.OK, again.err
    [entry] = again.json()["pull_requests"]
    assert entry["created"] is False and entry["number"] == 1
    assert len(github.pulls) == 1
    assert "PATCH /repos/acme/app/pulls/1" in github.paths()


def test_republishing_keeps_a_persons_edits_to_the_description(workspace, github, cli):
    """DF-296: the generated part sits between markers; a re-publish (`pr open`, or a
    resumed publish step) replaces only that part, never what a person wrote around it."""
    _, run_id = ship(cli)
    pull = github.pulls[0]
    assert pull["body"].startswith("<!-- agentic:run -->\n")
    assert pull["body"].rstrip().endswith("<!-- /agentic:run -->")
    pull["body"] = "Reviewer notes: check the CSV header.\n\n" + pull["body"] + "\nThanks!\n"
    assert cli("--json", "pr", "open", run_id).code == ExitCode.OK
    body = github.pulls[0]["body"]
    assert body.startswith("Reviewer notes: check the CSV header.\n\n<!-- agentic:run -->")
    assert body.endswith("<!-- /agentic:run -->\n\nThanks!\n")
    assert body.count("## Agentic run") == 1 and "Status: completed" in body


def test_the_pull_request_title_goes_through_the_redactor(workspace, github, monkeypatch, cli):
    """DF-295: a secret pasted into the run's request must not reach the host in the
    title (the body is redacted already)."""
    secret = "correct-horse-" + "battery-staple-42"  # gitleaks:allow (not a real secret)
    monkeypatch.setenv("ACME_DEPLOY_PASSWORD", secret)
    result = cli("--json", "run", "ship", "--input", f"request=CSV export {secret}")
    assert result.code == ExitCode.OK, result.err
    title = github.pulls[0]["title"]
    assert secret not in title and title.startswith("ship: CSV export")


def test_a_secret_across_the_title_length_limit_is_redacted_before_the_cut(
    workspace, github, monkeypatch, cli
):
    """Review F1: the title is cut to 100 characters; redacting after the cut would leave
    the head of a secret that straddles the limit (no longer a whole value to match)."""
    secret = "correct-horse-" + "battery-staple-42"  # gitleaks:allow (not a real secret)
    monkeypatch.setenv("ACME_DEPLOY_PASSWORD", secret)
    request = "x" * 78 + " " + secret  # the secret starts at title position 85
    result = cli("--json", "run", "ship", "--input", f"request={request}")
    assert result.code == ExitCode.OK, result.err
    title = github.pulls[0]["title"]
    assert secret[:8] not in title, title
    assert len(title) <= publishing.TITLE_LIMIT and title.startswith("ship: xxx")


def test_an_ssh_alias_remote_is_told_how_to_declare_it(workspace, github, monkeypatch, cli):
    """DF-299: `git@github.com-work:` (an alias for a second account in ~/.ssh/config) is
    not a host anyone named; the hint says how to declare it with kind and api_url."""
    git(workspace, "remote", "set-url", "origin", "git@github.com-work:acme/app.git")
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    assert result.code == ExitCode.INVALID
    assert "github.com-work" in result.out and "alias" in result.out
    assert "api_url" in result.out


def test_the_control_plane_link_is_used_when_one_is_configured(workspace, github, monkeypatch, cli):
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_URL", "https://agentic.acme.example/")
    _, run_id = ship(cli)
    body = github.pulls[0]["body"]
    link = f"https://agentic.acme.example/runs/{run_id}"
    assert f"[{run_id}]({link})" in body
    assert f"[requirements]({link}/handoffs/requirements-1.md)" in body
    assert f"- Logs: [step logs]({link}/logs)\n" in body


@pytest.mark.parametrize(
    "runs, statuses, state, code",
    [
        ([success("build"), success("lint")], [], "green", ExitCode.OK),
        ([success("build")], [{"context": "jenkins", "state": "failure"}], "red", ExitCode.ERROR),
        (
            [{"name": "e2e", "status": "in_progress", "conclusion": None}],
            [],
            "pending",
            ExitCode.ERROR,
        ),
        ([], [], "none", ExitCode.ERROR),
    ],
)
def test_pr_checks_reads_ci_and_records_the_verdict(
    workspace, github, cli, runs, statuses, state, code
):
    _, run_id = ship(cli)
    github.check_runs, github.statuses = [runs], statuses
    result = cli("--json", "pr", "checks", run_id)
    assert result.code == code, result.err
    assert result.json()["state"] == state
    recorded = json.loads((run_folder(workspace, run_id) / "ci.json").read_text("utf-8"))
    assert recorded["state"] == state and recorded["run_id"] == run_id


def test_watch_waits_until_the_checks_settle(workspace, github, monkeypatch, cli):
    _, run_id = ship(cli)
    running = [{"name": "build", "status": "queued", "conclusion": None}]
    github.check_runs = [[], running, running, [success("build")]]
    naps = []
    monkeypatch.setattr(publishing, "sleep", naps.append)
    result = cli("--json", "pr", "checks", run_id, "--watch", "--interval", "5")
    assert result.code == ExitCode.OK, result.err
    assert result.json()["state"] == "green" and naps == [5, 5, 5]


def test_watch_reports_none_after_the_grace_period(workspace, github, monkeypatch, cli):
    _, run_id = ship(cli)
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(publishing, "monotonic", lambda: next(clock))
    monkeypatch.setattr(publishing, "sleep", lambda seconds: None)
    result = cli("--json", "pr", "checks", run_id, "--watch", "--timeout", "1800")
    shown = result.json()
    assert result.code == ExitCode.ERROR and shown["state"] == "none"
    assert shown["timed_out"] is False  # settled as none, it did not wait the full timeout


def test_watch_gives_up_at_the_timeout_and_says_so(workspace, github, monkeypatch, cli):
    _, run_id = ship(cli)
    github.check_runs = [[{"name": "slow", "status": "in_progress", "conclusion": None}]]
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(publishing, "monotonic", lambda: next(clock))
    monkeypatch.setattr(publishing, "sleep", lambda seconds: None)
    result = cli("--json", "pr", "checks", run_id, "--watch", "--timeout", "300")
    assert result.json()["state"] == "pending" and result.json()["timed_out"] is True


def test_a_single_read_of_pending_ci_did_not_wait_and_does_not_say_so(workspace, github, cli):
    _, run_id = ship(cli)
    github.check_runs = [[{"name": "e2e", "status": "in_progress", "conclusion": None}]]
    shown = cli("--json", "pr", "checks", run_id).json()
    assert shown["state"] == "pending" and shown["timed_out"] is False
    recorded = json.loads((run_folder(workspace, run_id) / "ci.json").read_text("utf-8"))
    assert recorded["timed_out"] is False
    human = cli("pr", "checks", run_id)
    assert human.code == ExitCode.ERROR
    assert "still running" in human.out and "gave up" not in human.out


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_watch_settles_on_a_skipped_check_and_names_it(
    workspace, github, monkeypatch, cli, conclusion
):
    _, run_id = ship(cli)
    skipped = {"name": "deploy", "status": "completed", "conclusion": conclusion}
    github.check_runs = [[success("build"), skipped]]
    naps = []
    clock = iter(range(0, 100_000, 30))
    monkeypatch.setattr(publishing, "monotonic", lambda: next(clock))
    monkeypatch.setattr(publishing, "sleep", naps.append)
    result = cli("--json", "pr", "checks", run_id, "--watch", "--interval", "5")
    shown = result.json()
    assert result.code == ExitCode.ERROR  # skipped is not a pass (Zyroflo's rule)...
    assert shown["timed_out"] is False and naps == []  # ...but nothing is left to wait for
    assert shown["skipped"] == ["deploy"] and shown["running"] == []
    assert "skipped" in shown["why"] and "still running" not in shown["why"]


def test_watch_rides_out_a_passing_host_failure(workspace, github, monkeypatch, cli):
    _, run_id = ship(cli)
    github.check_runs = [[success("build")]]
    github.fail_next["GET check-runs"] = [(502, "bad gateway"), (503, "unavailable")]
    naps = []
    monkeypatch.setattr(publishing, "sleep", naps.append)
    result = cli("--json", "pr", "checks", run_id, "--watch", "--interval", "5")
    assert result.code == ExitCode.OK, result.err
    assert result.json()["state"] == "green" and naps == [5, 5]


def test_watch_gives_up_when_the_host_keeps_failing(workspace, github, monkeypatch, cli):
    _, run_id = ship(cli)
    github.fail["GET check-runs"] = (502, "bad gateway")
    monkeypatch.setattr(publishing, "sleep", lambda seconds: None)
    result = cli("--json", "pr", "checks", run_id, "--watch", "--interval", "5")
    assert result.code == ExitCode.ERROR
    error = json.loads(result.err)
    assert "HTTP 502" in error["error"] and "try again" in error["hint"]
    reads = [p for p in github.paths() if p.endswith("/check-runs")]
    assert len(reads) == publishing.MAX_TRANSIENT_FAILURES


def test_watch_stops_at_once_on_a_refusal(workspace, github, monkeypatch, cli):
    _, run_id = ship(cli)
    github.fail["GET check-runs"] = (404, "Not Found")
    monkeypatch.setattr(publishing, "sleep", lambda seconds: None)
    result = cli("--json", "pr", "checks", run_id, "--watch")
    assert result.code == ExitCode.ERROR and "HTTP 404" in json.loads(result.err)["error"]
    assert len([p for p in github.paths() if p.endswith("/check-runs")]) == 1


def test_the_push_wall_refuses_before_pushing(workspace, github, bare, cli):
    write(workspace / ".sdlc" / "policy.yml", "push_remote: upstream\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "runs publish to upstream only")
    result, run_id = ship(cli)
    assert result.code != ExitCode.OK
    shown = result.json()
    assert shown["status"] == "paused" and "upstream" in shown["pending"]["reason"]
    assert git(bare, "branch", "--list", f"sdlc/{run_id}") == ""  # nothing left the machine
    assert github.pulls == [] and "POST /repos/acme/app/pulls" not in github.paths()


def test_the_token_never_reaches_output_logs_or_the_run_folder(workspace, github, cli, caplog):
    caplog.set_level(logging.DEBUG)
    github.fail["POST pulls"] = (500, "upstream exploded")
    result, run_id = ship(cli)
    assert result.json()["status"] == "paused"
    assert "HTTP 500" in result.json()["pending"]["reason"]
    assert any(TOKEN in r["auth"] for r in github.requests)  # it did reach the API...
    shown = result.out + result.err + caplog.text
    assert TOKEN not in shown  # ...and nowhere else
    for path in run_folder(workspace, run_id).rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path

    del github.fail["POST pulls"]  # fixed: resuming retries the action
    resumed = cli("--json", "resume", run_id)
    assert resumed.code == ExitCode.OK, resumed.err
    assert len(github.pulls) == 1


def test_a_rejected_token_says_how_to_fix_it(workspace, github, monkeypatch, cli):
    _, run_id = ship(cli)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", "ghp_" + "wrong" + "0123456789")
    result = cli("--json", "pr", "checks", run_id)
    assert result.code == ExitCode.ERROR
    error = json.loads(result.err)
    assert "rejected" in error["error"] and "AGENTIC_GITHUB_TOKEN" in error["hint"]


def test_a_missing_token_stops_the_run_before_anything_is_spent(
    workspace, github, monkeypatch, cli
):
    monkeypatch.delenv("AGENTIC_GITHUB_TOKEN")
    gateway = FakeGateway({"ba": [answer("requirements")]})
    monkeypatch.setattr(runtime, "network_transport", gateway.transport)
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    assert result.code == ExitCode.INVALID
    assert "publish" in result.out and "agentic login --for github" in result.out
    assert gateway.requests == []  # no model call was paid for
    assert github.requests == [] and github.pulls == []


def test_an_unknown_host_stops_the_run_before_anything_is_spent(
    workspace, github, monkeypatch, cli
):
    git(workspace, "remote", "set-url", "origin", "https://git.unknown.example/acme/app.git")
    gateway = FakeGateway({"ba": [answer("requirements")]})
    monkeypatch.setattr(runtime, "network_transport", gateway.transport)
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    assert result.code == ExitCode.INVALID
    assert "git.unknown.example" in result.out and "git_hosts" in result.out
    assert gateway.requests == []


def test_a_failed_publish_step_keeps_what_to_do_in_its_reason(workspace, github, monkeypatch, cli):
    def refused(*args, **kwargs):
        raise CliError("no token for github.example.test", ExitCode.REFUSED, hint="do this")

    monkeypatch.setattr(publishing, "publish_run", refused)
    result, _ = ship(cli)
    reason = result.json()["pending"]["reason"]
    assert "no token for github.example.test" in reason and "do this" in reason


def test_pr_checks_before_any_pull_request_says_what_to_do(workspace, github, cli):
    paused = cli("--json", "run", "deliver", "--input", "request=x")
    run_id = paused.json()["run_id"]
    result = cli("--json", "pr", "checks", run_id)
    assert result.code == ExitCode.REFUSED
    assert "agentic pr open" in json.loads(result.err)["hint"]


def test_pr_open_refuses_a_run_that_has_not_finished(workspace, github, cli):
    paused = cli("--json", "run", "deliver", "--input", "request=x")
    result = cli("--json", "pr", "open", paused.json()["run_id"])
    assert result.code == ExitCode.REFUSED and github.pulls == []
