"""A real checkpoint commit carries the six audit trailers, as git itself reads them
(M1-45; spec §14.5)."""

import subprocess
from pathlib import Path

from agentic.telemetry.trailers import TRAILER_KEYS, CommitAudit, commit_message
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import StepCheckpoints

RUN = "r-trailers-1"
GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]


def git(where: Path, *args: str, stdin: str | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT_USER, *args],
        capture_output=True,
        text=True,
        check=True,
        input=stdin,
    )
    return done.stdout.strip()


def test_git_reads_all_six_trailers_on_a_checkpoint_commit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    RunLedger(repo, RUN).open(
        identity=RunIdentity(team="platform", policy_version="git:" + "a" * 40),
        kind="feature",
        project="demo",
        owner="platform",
        criticality="low",
    )
    checkpoints = StepCheckpoints(repo, RUN)
    tree = checkpoints.prepare()
    checkpoints.begin(fresh=True)
    (tree / "app.py").write_text("x = 2\n", encoding="utf-8")
    audit = CommitAudit(
        run_id=RUN,
        step="develop",
        iteration=2,
        agent="developer@1.2.0",
        triggered_by="asha@example.com",
        approved_by="ravi@example.com",
        approval_gate="review-approval",
        handoffs=("handoffs/design-1.md",),
    )
    head = checkpoints.finish(commit_message(f"agentic: develop #2 ({RUN})", audit))

    body = git(tree, "log", "-1", "--format=%B", next(iter(head.values())))
    parsed = git(tree, "interpret-trailers", "--parse", stdin=body).splitlines()
    keys = [line.split(":", 1)[0] for line in parsed]
    assert keys == list(TRAILER_KEYS)
    assert "Agentic-Approved-By: ravi@example.com (gate: review-approval)" in parsed
