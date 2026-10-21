"""Pushing one branch with plain git (M1-58): never forced, no prompt, output scrubbed."""

import subprocess
from pathlib import Path

import pytest

from agentic.hosting import HostingError
from agentic.hosting.git import push_branch, remote_url, rev_parse

GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev", "-c", "commit.gpgsign=false"]


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    git(work, "remote", "add", "origin", str(bare))
    return work, bare


def test_the_branch_arrives_on_the_remote(pair):
    work, bare = pair
    git(work, "switch", "-qc", "sdlc/r-1")
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "change")
    push_branch(work, "origin", "sdlc/r-1")
    assert git(bare, "rev-parse", "refs/heads/sdlc/r-1") == rev_parse(work, "sdlc/r-1")


def test_a_diverged_remote_branch_is_refused_not_forced(pair, tmp_path):
    work, bare = pair
    push_branch(work, "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "c.txt").write_text("c\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "theirs")
    git(other, "push", "-q", "origin", "main")
    (work / "d.txt").write_text("d\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "ours")
    theirs = git(bare, "rev-parse", "refs/heads/main")
    with pytest.raises(HostingError, match="push"):
        push_branch(work, "origin", "main")
    assert git(bare, "rev-parse", "refs/heads/main") == theirs  # never overwritten


@pytest.mark.parametrize("remote, branch", [("--mirror", "main"), ("origin", "-f")])
def test_an_option_shaped_name_is_refused(pair, remote, branch):
    work, _ = pair
    with pytest.raises(HostingError, match="refused"):
        push_branch(work, remote, branch)


def test_credentials_in_a_failing_url_are_scrubbed(pair):
    work, _ = pair
    secret = "s3cr3t" + "Value"
    git(work, "remote", "add", "broken", f"https://user:{secret}@127.0.0.1:9/acme/app.git")
    with pytest.raises(HostingError) as raised:
        push_branch(work, "broken", "main", timeout=60)
    assert secret not in str(raised.value)


def test_a_known_secret_is_scrubbed_from_the_message(pair):
    work, _ = pair
    with pytest.raises(HostingError) as raised:
        push_branch(work, "nowhere", "main", secrets=["nowhere"])
    assert "nowhere" not in str(raised.value)


def test_a_pre_push_hook_never_sees_the_frameworks_secrets(pair, tmp_path, monkeypatch):
    """A husky hook, credential helper or ssh wrapper runs inside `git push`: it gets the
    person's own credentials (an SSH agent), never the framework's tokens (DF-294)."""
    work, _ = pair
    # a credential's name is the org's choice (`acme_ghe`): it need not look secret (F3)
    secrets = ["AGENTIC_GITHUB_TOKEN", "AGENTIC_TOKENLEAN_API_KEY", "AGENTIC_ACME_GHE"]
    for name in secrets:
        monkeypatch.setenv(name, "x" * 16)
    monkeypatch.setenv("AGENTIC_HOME", "/home/dev/.agentic")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    seen = tmp_path / "hook-env.txt"
    hook = work / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\nenv > '{seen.as_posix()}'\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)

    push_branch(work, "origin", "main")

    names = {line.split("=", 1)[0] for line in seen.read_text(encoding="utf-8").splitlines()}
    assert not names & set(secrets)
    assert {"AGENTIC_HOME", "SSH_AUTH_SOCK"} <= names


def test_remote_url_reads_the_fetch_url(pair):
    work, bare = pair
    assert Path(remote_url(work, "origin")) == bare
    with pytest.raises(HostingError, match="no remote"):
        remote_url(work, "missing")
