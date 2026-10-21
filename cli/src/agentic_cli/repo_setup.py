"""The shared files `agentic init --repo` proposes, and the branch they go on (M1-52).

`shared_files` decides exactly what is proposed (spec §4.2, §24):

  * `.agentic/config.yaml` - the repo's classification, team, pipelines and components;
  * `AGENTS.md` - a first draft from a scan of the repo, for people to edit and review;
    only when the repo has none (a curated one, in any letter case, is never replaced);
  * `.gitattributes` - the repo's own file, byte for byte and with its own line endings,
    with an agentic block (LF for definitions, generated-index marking) added or replaced;
  * `.github/workflows/agentic-index-export.yml` - only when org policy sets
    `index_export_workflow` (D39: the export is opt-in).

Never `.agentic/pipelines/`: pipelines are managed in the policy repo (D33), and a repo
pipeline is added later by `agentic pipelines new`, which honours `allow_repo_pipelines`.

`commit_on_branch` builds the commit with git plumbing and a scratch index - read the
base tree, add the files' blobs, write the tree, commit it - and points a new branch at
it. The working tree, the real index and the current branch never change, and no git
hook runs. `agentic init --repo` then pushes the branch and opens its pull request
through the hosting adapter (M1-58, DF-183; `pr_body` describes the files); without a
token or a known host - or with `--no-pr` - `next_steps` says how to do it by hand.
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
    """Path -> text of every file the branch adds or changes.

    New files are LF. The repo's own `.gitattributes` keeps its bytes and line endings
    (decoded with surrogateescape, so a non-UTF-8 file round-trips). An AGENTS.md already
    in the repo, in any letter case, is left alone (`existing_agents_md`)."""
    files = {CONFIG_REL: render_repo_config(config)}
    if existing_agents_md(repo) is None:
        files["AGENTS.md"] = agents_md(repo)
    attributes = _head_blob(repo, ".gitattributes") or b""
    files[".gitattributes"] = merge_gitattributes(attributes.decode("utf-8", "surrogateescape"))
    if policy.index_export_workflow:
        files[WORKFLOW_REL] = INDEX_EXPORT_WORKFLOW
    return files


def existing_agents_md(repo: Path) -> str | None:
    """The name of the AGENTS.md committed at the repo root (any letter case), or None.

    Windows and macOS checkouts have one path for AGENTS.md and agents.md, so a curated
    file under either name is kept, never replaced by the scan's draft."""
    names = _git_out(repo, "ls-tree", "-z", "--name-only", "HEAD").split("\0")
    return next((name for name in names if name.lower() == "agents.md"), None)


def merge_gitattributes(existing: str) -> str:
    """`existing` with the agentic block added, or replaced if it is already there; the
    file's own line ending (CRLF or LF) is kept."""
    eol = "\r\n" if "\r\n" in existing else "\n"
    lines = existing.replace("\r\n", "\n").split("\n") if existing else []
    if AGENTIC_BLOCK_BEGIN in lines:
        start = lines.index(AGENTIC_BLOCK_BEGIN)
        end = lines.index(AGENTIC_BLOCK_END, start) if AGENTIC_BLOCK_END in lines else start
        lines = lines[:start] + lines[end + 1 :]
    kept = eol.join(lines).rstrip("\r\n")
    block = eol.join([AGENTIC_BLOCK_BEGIN, *AGENTIC_ATTRIBUTES, AGENTIC_BLOCK_END])
    return f"{kept}{eol}{block}{eol}" if kept else f"{block}{eol}"


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
    """Commit `files` on top of HEAD on a new branch; returns (branch, commit). In a
    repository with no commit yet, the branch starts with them (a root commit)."""
    parent = ["-p", "HEAD"] if _git_out(repo, "rev-parse", "--verify", "-q", "HEAD") else []
    with tempfile.TemporaryDirectory(prefix="agentic-init-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        if parent:
            _git_run(repo, env, "read-tree", "HEAD")
        for path, text in sorted(files.items()):
            data = text.encode("utf-8", "surrogateescape")  # the repo's own bytes round-trip
            blob = _git_run(repo, env, "hash-object", "-w", "--stdin", data=data)
            _git_run(repo, env, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = _git_run(repo, env, "write-tree")
        commit = _git_run(repo, env, "commit-tree", tree, *parent, "-m", message)
    for n in range(1, 100):
        branch = BRANCH if n == 1 else f"{BRANCH}-{n}"
        if not _git_out(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
            # the zero old value: create only, never move an existing branch
            _git_run(repo, None, "update-ref", f"refs/heads/{branch}", commit, "0" * 40)
            return branch, commit
    raise CliError(f"too many {BRANCH}-<n> branches already exist", ExitCode.ERROR)


PR_TITLE = "Set up agentic for this repository"
#: What each proposed file is, for the pull request's description.
FILE_PURPOSES = {
    CONFIG_REL: "the repository's classification, owning team and enabled pipelines",
    "AGENTS.md": "a first draft from a scan of the repository - please edit it before merging",
    ".gitattributes": "an agentic block: LF endings for definitions, the index marked generated",
    WORKFLOW_REL: "the nightly code-intelligence index export (org policy turns it on)",
}


def pr_body(files: list[str], kept: list[str]) -> str:
    """The description of the pull request `agentic init --repo` opens."""
    lines = [
        "## Set up agentic for this repository",
        "",
        "Opened by `agentic init --repo`. It adds the repository's shared agentic files and",
        "changes nothing else.",
        "",
    ]
    lines += [f"- `{path}` - {FILE_PURPOSES.get(path, 'a shared agentic file')}" for path in files]
    if kept:
        lines += ["", "Kept as they are (no draft proposed): " + ", ".join(f"`{k}`" for k in kept)]
    lines += [
        "",
        "Review the files, edit what needs editing on this branch, and merge when ready.",
        "Pipelines are managed in the org policy repository, not here.",
    ]
    return "\n".join(lines) + "\n"


def first_commit_steps(default_branch: str, branch: str) -> dict[str, str]:
    """A repository with no commit yet has no base branch to open a pull request
    against: the setup branch becomes its first commit (M1-70, owner decision 65)."""
    return {
        "merge": f"git merge --ff-only {branch}",
        "push": f"git push -u origin {default_branch}",
    }


def next_steps(
    origin: str | None, default_branch: str, branch: str, *, local: bool = False
) -> dict[str, str]:
    """How the branch reaches the default branch: push it and open a pull request; with
    `local` (standalone, or no origin), merging it into this clone is offered as well."""
    push = f"git push -u origin {branch}"
    steps = {"push": push if origin else f"git remote add origin <url>, then {push}"}
    if origin and origin.startswith("github.com/"):
        steps["pull_request"] = f"https://{origin}/compare/{default_branch}...{branch}?expand=1"
    else:
        steps["pull_request"] = f"open a pull request from {branch} into {default_branch}"
    if local or not origin:
        steps["merge"] = f"git merge {branch}"
    return steps


def _git_out(repo: Path, *args: str) -> str:
    done = subprocess.run([*_GIT, "-C", str(repo), *args], capture_output=True)
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else ""


def _head_blob(repo: Path, path: str) -> bytes | None:
    """The bytes of `path` at HEAD, exactly as committed; None when it is not there."""
    if not _git_out(repo, "ls-tree", "-z", "--name-only", "HEAD", "--", path):
        return None
    done = subprocess.run(
        [*_GIT, "-C", str(repo), "cat-file", "blob", f"HEAD:{path}"], capture_output=True
    )
    if done.returncode != 0:  # never propose the block alone over a file we could not read
        error = done.stderr.decode("utf-8", "replace").strip()
        raise CliError(f"could not read {path} at HEAD: {error or 'git cat-file failed'}")
    return done.stdout


def _git_run(repo: Path, env: dict | None, *args: str, data: bytes | None = None) -> str:
    done = subprocess.run([*_GIT, "-C", str(repo), *args], input=data, capture_output=True, env=env)
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip().splitlines()
        hint = "set git config user.name and user.email" if args[0] == "commit-tree" else None
        raise CliError(f"git {args[0]} failed: {error[-1] if error else '?'}", hint=hint)
    return done.stdout.decode("utf-8").strip()
