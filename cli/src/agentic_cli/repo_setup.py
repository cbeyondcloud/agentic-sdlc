"""The shared files `agentic init --repo` proposes, and the branch they go on (M1-52).

`shared_files` decides exactly what is proposed (spec §4.2, §24):

  * `.agentic/config.yaml` - the repo's classification, team, pipelines and components;
  * `AGENTS.md` - a first draft from a scan of the repo, for people to edit and review;
  * `.gitattributes` - the repo's own file with an agentic block (LF for definitions,
    generated-index marking) added or replaced;
  * `.github/workflows/agentic-index-export.yml` - only when org policy sets
    `index_export_workflow` (D39: the export is opt-in).

Never `.agentic/pipelines/`: pipelines are managed in the policy repo (D33), and a repo
pipeline is added later by `agentic pipelines new`, which honours `allow_repo_pipelines`.

`commit_on_branch` builds the commit with git plumbing and a scratch index - read the
base tree, add the files' blobs, write the tree, commit it - and points a new branch at
it. The working tree, the real index and the current branch never change, and no git
hook runs. Pushing and opening the pull request are left to the person (or to the
GitHub adapter, M1-58): `next_steps` says how.
"""

import os
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from agentic.policy import OrgPolicy
from agentic.repoconfig import CONFIG_REL, RepoConfig, render_repo_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

BRANCH = "agentic/init"
WORKFLOW_REL = ".github/workflows/agentic-index-export.yml"
AGENTIC_BLOCK_BEGIN = "# agentic: begin (added by `agentic init --repo`)"
AGENTIC_BLOCK_END = "# agentic: end"
AGENTIC_ATTRIBUTES = (
    ".agentic/**/*.md text eol=lf",
    ".agentic/**/*.yaml text eol=lf",
    "AGENTS.md text eol=lf",
    ".agentic/index/** linguist-generated -diff",
)
LANGUAGES = {
    ".py": "Python",
    ".java": "Java",
    ".kt": "Kotlin",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".scala": "Scala",
    ".swift": "Swift",
    ".c": "C",
    ".cpp": "C++",
    ".sql": "SQL",
}
_GIT = ["git", "-c", "core.fsmonitor=false"]

INDEX_EXPORT_WORKFLOW = """\
# Nightly code-intelligence index export (spec 10.3, D39): built once by CI and published
# outside main, as a workflow artifact, for developers to import. Added by
# `agentic init --repo` because org policy sets index_export_workflow. The export
# command and its pinned format are delivered by the framework's index export (M2-44).
name: agentic-index-export
on:
  schedule:
    - cron: "17 2 * * *"
  workflow_dispatch: {}
permissions:
  contents: read
jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv tool install agentic
      - run: agentic index export --out agentic-index-export
      - uses: actions/upload-artifact@v4
        with:
          name: agentic-index-export
          path: agentic-index-export
"""


def shared_files(repo: Path, policy: OrgPolicy, config: RepoConfig) -> dict[str, str]:
    """Path -> LF text of every file the branch adds or changes."""
    files = {
        CONFIG_REL: render_repo_config(config),
        "AGENTS.md": agents_md(repo),
        ".gitattributes": merge_gitattributes(_git_out(repo, "show", "HEAD:.gitattributes")),
    }
    if policy.index_export_workflow:
        files[WORKFLOW_REL] = INDEX_EXPORT_WORKFLOW
    return files


def merge_gitattributes(existing: str) -> str:
    """`existing` with the agentic block added, or replaced if it is already there."""
    lines = existing.replace("\r\n", "\n").split("\n") if existing else []
    if AGENTIC_BLOCK_BEGIN in lines:
        start = lines.index(AGENTIC_BLOCK_BEGIN)
        end = lines.index(AGENTIC_BLOCK_END, start) if AGENTIC_BLOCK_END in lines else start
        lines = lines[:start] + lines[end + 1 :]
    kept = "\n".join(lines).rstrip("\n")
    block = "\n".join([AGENTIC_BLOCK_BEGIN, *AGENTIC_ATTRIBUTES, AGENTIC_BLOCK_END])
    return f"{kept}\n{block}\n" if kept else f"{block}\n"


def agents_md(repo: Path) -> str:
    """A first AGENTS.md from the committed files: top-level folders and main languages."""
    tracked = _git_out(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    folders = sorted({path.split("/", 1)[0] for path in tracked if "/" in path})
    counts = Counter(LANGUAGES.get(Path(path).suffix.lower()) for path in tracked)
    counts.pop(None, None)
    languages = ", ".join(name for name, _ in counts.most_common(5)) or "not detected"
    layout = "\n".join(f"- `{name}/` - (describe)" for name in folders) or "- (describe)"
    return (
        "# AGENTS.md\n\n"
        "Guidance for AI agents (and new people) working in this repository. Agents read\n"
        "this file at the start of every run and may propose additions in their handoffs;\n"
        "changes land through normal pull request review.\n\n"
        "## Overview\n\n(What this repository is for, in two or three sentences.)\n\n"
        f"## Layout\n\n{layout}\n\n"
        f"## Languages\n\n{languages}\n\n"
        "## Build and test\n\n(The exact commands to build, test and lint.)\n\n"
        "## Conventions\n\n(Coding style, naming, review rules, things to never do.)\n"
    )


def commit_on_branch(repo: Path, files: dict[str, str], message: str) -> tuple[str, str]:
    """Commit `files` on top of HEAD on a new branch; returns (branch, commit)."""
    with tempfile.TemporaryDirectory(prefix="agentic-init-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git_run(repo, env, "read-tree", "HEAD")
        for path, text in sorted(files.items()):
            blob = _git_run(repo, env, "hash-object", "-w", "--stdin", data=text.encode("utf-8"))
            _git_run(repo, env, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = _git_run(repo, env, "write-tree")
        commit = _git_run(repo, env, "commit-tree", tree, "-p", "HEAD", "-m", message)
    for n in range(1, 100):
        branch = BRANCH if n == 1 else f"{BRANCH}-{n}"
        if not _git_out(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
            # the zero old value: create only, never move an existing branch
            _git_run(repo, None, "update-ref", f"refs/heads/{branch}", commit, "0" * 40)
            return branch, commit
    raise CliError(f"too many {BRANCH}-<n> branches already exist", ExitCode.ERROR)


def next_steps(origin: str | None, default_branch: str, branch: str) -> dict[str, str]:
    steps = {"push": f"git push -u origin {branch}"}
    if origin and origin.startswith("github.com/"):
        steps["pull_request"] = f"https://{origin}/compare/{default_branch}...{branch}?expand=1"
    else:
        steps["pull_request"] = f"open a pull request from {branch} into {default_branch}"
    return steps


def _git_out(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [*_GIT, "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )
    return done.stdout if done.returncode == 0 else ""


def _git_run(repo: Path, env: dict | None, *args: str, data: bytes | None = None) -> str:
    done = subprocess.run([*_GIT, "-C", str(repo), *args], input=data, capture_output=True, env=env)
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip().splitlines()
        hint = "set git config user.name and user.email" if args[0] == "commit-tree" else None
        raise CliError(f"git {args[0]} failed: {error[-1] if error else '?'}", hint=hint)
    return done.stdout.decode("utf-8").strip()
