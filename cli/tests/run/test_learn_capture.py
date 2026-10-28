"""The learning loop on the CLI (M3-11..M3-13): a gate rejection is recorded in the run's
feedback journal as the decision is taken; `learn capture-pr` reads the run's pull request
reviews (a local fake GitHub serving recorded answers); `learn attribute` corrects who
learns; `learn analyse` runs the Learning Analyst through the (fake) gateway; three similar
corrections make a standalone proposal. No real provider or host is ever called."""

import json
import subprocess
from pathlib import Path

import pytest
from _cli_support import FakeGateway, answer, git, write
from fake_github import FakeGitHub

from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0311"  # gitleaks:allow (not a real key)
TOKEN = "ghp_" + "Learn" + "Fake0123456789"  # gitleaks:allow - built from pieces
HOST = "github.example.test"
RECORDED = (
    Path(__file__).resolve().parents[3] / "core" / "tests" / "fixtures" / "hosting" / "github"
)
CRITERIA = [
    "Acceptance criteria must name every CSV column.",
    "Name every CSV column in the acceptance criteria.",
    "The acceptance criteria should name each CSV column.",
]
ANALYST_TYPE = """  learning-analyst:
    description: The learning loop's analyst.
    tier: fast
    backends: [native]
    tools: []
    consumes: []
    limits:
      max: { budget_usd: 0.2, max_turns: 3, max_minutes: 5 }
      children_max: { max_total: 0, max_concurrent: 0, max_depth: 0,
                      per_child: { budget_usd: 0, max_turns: 0, max_minutes: 0 } }
"""
ANALYST = (
    "name: learning-analyst\ntype: learning-analyst\nversion: 1.0.0\n"
    "description: Says which agents learn from a correction.\n"
)


def gateway_with(monkeypatch, answers) -> FakeGateway:
    fake = FakeGateway(answers)
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    attempts = [answer("requirements", summary=f"Attempt {n}.") for n in range(1, 6)]
    return gateway_with(monkeypatch, {"ba": attempts, "architect": [answer("design")]})


def started(cli) -> str:
    result = cli("--json", "run", "deliver", "--input", "request=CSV export")
    assert result.code == ExitCode.PAUSED, result.err
    return result.json()["run_id"]


def journal(workspace: Path, run_id: str) -> list[dict]:
    path = workspace / ".agentic" / "local" / "runs" / run_id / "feedback.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_reject_on_the_cli_records_feedback_in_the_run_folder(workspace, gateway, cli):
    run_id = started(cli)
    result = cli("--json", "reject", run_id, "--feedback", CRITERIA[0])
    assert result.code == ExitCode.PAUSED, result.err

    entries = journal(workspace, run_id)
    assert [e["event"] for e in entries] == ["learning.feedback_captured"]
    captured = entries[0]
    assert (captured["signal"], captured["gate"], captured["kind"]) == (
        "gate-rejection",
        "requirements-approval",
        "requirements",
    )
    assert captured["text"] == CRITERIA[0] and captured["agent"] == "ba@1.0.0"
    assert captured["attribution"]["learners"] == [{"agent": "ba", "confidence": 1.0}]
    assert captured["lesson"] == CRITERIA[0]
    listed = cli("--json", "learn", "feedback", "--run", run_id)
    assert listed.code == ExitCode.OK, listed.err
    assert [(r["signal"], r["agents"]) for r in listed.json()] == [("gate-rejection", ["ba"])]


def test_three_similar_rejections_create_a_standalone_proposal(workspace, gateway, cli):
    run_id = started(cli)
    for text in CRITERIA[:2]:
        assert cli("--json", "reject", run_id, "--feedback", text).code == ExitCode.PAUSED
    assert cli("--json", "learn", "proposals").json() == []

    assert cli("--json", "reject", run_id, "--feedback", CRITERIA[2]).code == ExitCode.PAUSED
    proposals = cli("--json", "learn", "proposals").json()
    assert [(p["agent"], p["scope"], p["status"], len(p["evidence"])) for p in proposals] == [
        ("ba", "repo:local", "pending", 3)
    ]
    assert (workspace / ".agentic" / "local" / "learning" / "proposals.json").is_file()
    # nothing about the agent changed: a proposal is only created (D29)
    assert not list((workspace / ".agentic").glob("agents/*.md"))


def test_attribute_standalone_writes_a_correction(workspace, gateway, cli):
    run_id = started(cli)
    cli("--json", "reject", run_id, "--feedback", "the design is wrong, not the criteria")
    fid = journal(workspace, run_id)[0]["id"]

    done = cli(
        "--json", "learn", "attribute", fid, "--agent", "architect", "--reason", "a design call"
    )
    assert done.code == ExitCode.OK, done.err
    row = cli("--json", "learn", "feedback").json()[0]
    assert (row["agents"], row["method"]) == (["architect"], "human")
    assert [e["event"] for e in journal(workspace, run_id)][-1] == "learning.attribution_corrected"
    missing = cli("--json", "learn", "attribute", "fb-" + "0" * 20, "--agent", "x", "--reason", "y")
    assert missing.code == ExitCode.INVALID


def test_analyse_uses_the_learning_analyst(workspace, policy_repo, monkeypatch, cli):
    types = policy_repo / "policy" / "agent-types.yaml"
    types.write_text(types.read_text("utf-8") + ANALYST_TYPE, encoding="utf-8")
    write(policy_repo / "agents" / "learning-analyst.md", f"---\n{ANALYST}---\nYou analyse.\n")
    verdict = {
        "learners": [{"agent": "ba", "confidence": 0.9}],
        "lesson": "List every CSV column in the acceptance criteria.",
        "reason": "a requirements gap",
    }
    attempts = [answer("requirements", summary=f"Attempt {n}.") for n in range(1, 4)]
    fake = gateway_with(
        monkeypatch,
        {
            "ba": attempts,
            "architect": [answer("design")],
            "learning-analyst": [json.dumps(verdict)],
        },
    )
    run_id = started(cli)
    cli("--json", "reject", run_id, "--feedback", "Columns are missing. Add them.")

    done = cli("--json", "learn", "analyse", run_id)
    assert done.code == ExitCode.OK, done.err
    assert len(done.json()["analysed"]) == 1
    assert any(r["agent"] == "learning-analyst" for r in fake.requests)
    row = cli("--json", "learn", "feedback").json()[0]
    assert row["lesson"] == verdict["lesson"]
    assert row["method"] == "rules"  # the rules decided; the analyst wrote the lesson
    again = cli("--json", "learn", "analyse", run_id)
    assert again.json()["analysed"] == []  # nothing new to analyse


def test_a_failed_analysis_is_retried_by_the_next_analyse(workspace, policy_repo, monkeypatch, cli):
    """An analyst that gives no verdict (the gateway down, two invalid answers) leaves the
    record unanalysed: the command says so and fails, and the next `learn analyse` tries
    again instead of skipping the record for good."""
    types = policy_repo / "policy" / "agent-types.yaml"
    types.write_text(types.read_text("utf-8") + ANALYST_TYPE, encoding="utf-8")
    write(policy_repo / "agents" / "learning-analyst.md", f"---\n{ANALYST}---\nYou analyse.\n")
    verdict = {
        "learners": [{"agent": "ba", "confidence": 0.9}],
        "lesson": "List every CSV column in the acceptance criteria.",
        "reason": "a requirements gap",
    }
    attempts = [answer("requirements", summary=f"Attempt {n}.") for n in range(1, 4)]
    gateway_with(
        monkeypatch,
        {
            "ba": attempts,
            "architect": [answer("design")],
            "learning-analyst": ["not json", "not json", json.dumps(verdict)],
        },
    )
    run_id = started(cli)
    cli("--json", "reject", run_id, "--feedback", "Columns are missing. Add them.")
    fid = journal(workspace, run_id)[0]["id"]

    failed = cli("--json", "learn", "analyse", run_id)
    assert failed.code == ExitCode.ERROR, failed.err
    assert failed.json() == {"analysed": [], "failed": [fid]}
    assert [e["event"] for e in journal(workspace, run_id)] == ["learning.feedback_captured"]

    again = cli("--json", "learn", "analyse", run_id)
    assert again.code == ExitCode.OK, again.err
    assert again.json() == {"analysed": [fid], "failed": []}
    assert cli("--json", "learn", "feedback").json()[0]["lesson"] == verdict["lesson"]


def test_analyse_without_the_analyst_agent_explains(workspace, gateway, cli):
    run_id = started(cli)
    cli("--json", "reject", run_id, "--feedback", "x")
    refused = cli("--json", "learn", "analyse", run_id)
    assert refused.code == ExitCode.REFUSED
    assert "learning-analyst" in refused.err


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


@pytest.fixture
def github(tmp_path, workspace, policy_repo, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(workspace, "remote", "add", "origin", f"https://{HOST}/acme/app.git")
    git(workspace, "remote", "set-url", "--push", "origin", str(remote))
    git(workspace, "push", "-q", "origin", "main")
    fake = FakeGitHub(TOKEN, remote).start()
    write(
        tmp_path / "home" / "config.yaml",
        f"git_hosts:\n  {HOST}:\n    kind: github\n    api_url: {fake.url}\n",
    )
    write(policy_repo / "pipelines" / "ship.yaml", SHIP)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    gateway_with(monkeypatch, {"ba": [answer("requirements")]})
    yield fake
    fake.stop()


def test_capture_pr_records_reviews_and_misses(workspace, github, cli):
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    assert result.code == ExitCode.OK, result.err
    run_id = result.json()["run_id"]
    pull = github.pulls[0]
    pull["recorded_reviews"] = json.loads((RECORDED / "pr-7-reviews.json").read_text("utf-8"))
    pull["recorded_comments"] = json.loads((RECORDED / "pr-7-comments.json").read_text("utf-8"))

    done = cli("--json", "learn", "capture-pr", run_id)
    assert done.code == ExitCode.OK, done.err
    recorded = done.json()["recorded"]
    assert sorted(r["actor"] for r in recorded) == ["alice", "alice", "carol", "carol"]
    assert {r["signal"] for r in recorded} == {"pr-review"}  # no review step: nothing missed
    assert "GET /repos/acme/app/pulls/1/reviews" in github.paths()
    rows = {r["text"]: r for r in cli("--json", "learn", "feedback").json()}
    inline = rows["Wrap this call in our `with_retry()` helper."]
    assert inline["agents"] == ["developer", "code-review"]
    tests = rows["There is no test for the empty export case."]
    assert tests["agents"][0] == "qa"
    again = cli("--json", "learn", "capture-pr", run_id)
    assert again.json()["recorded"] == []  # the same comments are recorded once


def test_capture_pr_reads_each_pull_request_in_its_own_repository(workspace, github, cli):
    """A component's pull request may live in another repository than the workspace's:
    it is read there, and its records name that repository."""
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    assert result.code == ExitCode.OK, result.err
    run_id = result.json()["run_id"]
    pr_file = workspace / ".agentic" / "local" / "runs" / run_id / "pr.json"
    record = json.loads(pr_file.read_text("utf-8"))
    record["pull_requests"][0]["repo"] = "acme/web"
    pr_file.write_text(json.dumps(record), encoding="utf-8")
    pull = github.pulls[0]
    pull["recorded_comments"] = json.loads((RECORDED / "pr-7-comments.json").read_text("utf-8"))

    done = cli("--json", "learn", "capture-pr", run_id)
    assert done.code == ExitCode.OK, done.err
    assert done.json()["recorded"]
    assert "GET /repos/acme/web/pulls/1/comments" in github.paths()
    assert "GET /repos/acme/app/pulls/1/comments" not in github.paths()
    assert {e["repo"] for e in journal(workspace, run_id)} == {f"{HOST}/acme/web"}


def test_the_pull_request_authors_own_comments_are_not_corrections(workspace, github, cli):
    """The agent-made pull request is opened with the developer's own token: what its
    author writes there explains the change, it does not correct an agent."""
    result = cli("--json", "run", "ship", "--input", "request=CSV export")
    assert result.code == ExitCode.OK, result.err
    run_id = result.json()["run_id"]
    pull = github.pulls[0]
    own = {"user": {"login": github.login}, "submitted_at": "2026-10-24T09:00:00Z"}
    pull["recorded_reviews"] = [
        {**own, "id": 901, "state": "COMMENTED", "body": "I split this into two commits."}
    ]
    pull["recorded_comments"] = [
        {
            "id": 902,
            "pull_request_review_id": 901,
            "user": {"login": github.login},
            "body": "note: renamed this helper",
            "path": "a.py",
            "line": 3,
            "created_at": "2026-10-24T09:00:00Z",
        }
    ]

    done = cli("--json", "learn", "capture-pr", run_id)
    assert done.code == ExitCode.OK, done.err
    assert done.json()["recorded"] == []
