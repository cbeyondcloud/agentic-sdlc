"""A repository Zyroflo makes in place (`vcs.deny_raw_git`) with its own commit tool, for the
commit-command override tests (M2-61). The tool is a Python script committed in the repo:
`commit <message file>`, `restore <commit>`, `push <branch>`; it uses git itself, as a real
repository's tooling may - the framework never does."""

import subprocess
import sys
from pathlib import Path

from sdlc_commons.ledger import RunIdentity, RunLedger

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)
RUN = "r-inplace-1"
GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]
#: The interpreter by a quoted absolute path, so the tool runs on Windows and POSIX alike.
PY = Path(sys.executable).as_posix()
TOOL_PREFIX = f'"{PY}" tools/vcs.py'
ALLOW = (f"{TOOL_PREFIX} commit", f"{TOOL_PREFIX} restore", f"{TOOL_PREFIX} push")
COMMIT = f"{TOOL_PREFIX} commit {{message_file}}"
ROLLBACK = f"{TOOL_PREFIX} restore {{commit}}"
PUSH = f"{TOOL_PREFIX} push {{branch}}"
ROLLBACKS = {".": ROLLBACK}

TOOL = """import os
import subprocess
import sys

USER = ["-c", "user.email=tool@example.invalid", "-c", "user.name=Tool"]


def git(*args):
    subprocess.run(["git", *USER, *args], check=True)


action, value = sys.argv[1], sys.argv[2]
with open(os.path.join(os.environ["VCS_TOOL_LOG"]), "a", encoding="utf-8") as log:
    log.write(action + "\\n")
if action == "commit":
    git("add", "-A")
    git("commit", "-q", "-F", value)
elif action == "restore":
    git("reset", "-q", "--hard", value)
    git("clean", "-q", "-fd")
elif action == "push":
    git("push", "-q", "origin", "HEAD:refs/heads/" + value)
else:
    sys.exit(f"unknown action {action}")
"""


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT_USER, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def profile(commit: str = COMMIT, push: str = PUSH) -> str:
    lines = ["vcs:"]
    if commit:
        lines.append(f"  commit_command: '{commit}'")
    if push:
        lines.append(f"  push_command: '{push}'")
    lines.append("  deny_raw_git: true")
    return "\n".join(lines) + "\n"


def make_in_place_repo(path: Path, *, commit: str = COMMIT, push: str = PUSH) -> Path:
    """A git repository whose profile denies raw git and names the tool's commands."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (path / ".gitignore").write_text(
        ".venv/\n.agentic/local/\n.sdlc-worktrees/\n", encoding="utf-8"
    )
    (path / "tools").mkdir()
    (path / "tools" / "vcs.py").write_text(TOOL, encoding="utf-8", newline="\n")
    (path / ".sdlc").mkdir()
    (path / ".sdlc" / "profile.yml").write_text(profile(commit, push), encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


def open_ledger(target: Path, run_id: str = RUN) -> None:
    RunLedger(target, run_id).open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
