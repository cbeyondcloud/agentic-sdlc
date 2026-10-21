"""The repo-root and safety checks `init` runs before it writes anything (M1-50, spec §24).

`agentic init` and `agentic init --repo` refuse - exit code 2, a clear message, nothing
written - unless all eight hold:

  1. inside a git working tree (not a bare repo);
  2. the current directory IS the repo root. Compared as the same file
     (`os.path.samefile`), so case (Windows, macOS), 8.3 short names, symlinks and
     junctions all resolve, with a real-path comparison as the fallback;
  3. not inside a run worktree (`.agentic/local/worktrees/`, or Zyroflo's default
     `.sdlc-worktrees/`) and not a submodule - unless the superproject's
     `.agentic/config.yaml` lists the submodule in `components`;
  4. not a filesystem or drive root and not the user's home folder, even as a repo;
  5. org mode: the `origin` remote matches `allowed_repo_patterns` in org policy. An
     empty list allows nothing in org mode (it means "any repo" only standalone);
  6. developer `init`: `.agentic/config.yaml` is committed at HEAD with a
     classification the org policy defines. `init --repo`: it is NOT yet committed;
  7. `init --repo`: the user holds `agent.edit.repo`, the tree is clean, and HEAD is
     the default branch. Standalone has no RBAC; in org mode, until the control plane
     checks roles (M2-03), no permission check means refused;
  8. no other `.agentic/config.yaml` below the root (tracked or untracked, not ignored).

Every check only reads. Git runs with `core.fsmonitor` off and optional locks off, so a
repository's own config cannot run a command and `git status` writes nothing.
"""

import fnmatch
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentic.policy import OrgPolicy
from agentic.repoconfig import CONFIG_REL, RepoConfig, RepoConfigError, load_repo_config
from agentic.yamlio import YamlFileError, read_text
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

Mode = Literal["developer", "repo"]
PERMISSION = "agent.edit.repo"
RUN_TREE_DIRS = ((".agentic", "local", "worktrees"), (".sdlc-worktrees",))
_GIT = ["git", "-c", "core.fsmonitor=false"]


class GuardRefusal(CliError):
    """A failed safety check: always exit 2, and `check` says which of the eight."""

    def __init__(self, check: int, message: str, hint: str | None = None) -> None:
        super().__init__(message, ExitCode.REFUSED, hint=hint)
        self.check = check


@dataclass(frozen=True)
class RepoRoot:
    path: Path  # the repository's top level, as git reports it
    origin: str | None  # host/owner/repo of `origin`, or None
    default_branch: str | None
    config: RepoConfig | None  # developer mode: the committed `.agentic/config.yaml`
    empty: bool = False  # no commit yet (`git init` and nothing since)


def check_repo(
    cwd: Path,
    *,
    mode: Mode,
    home: Path,
    policy: OrgPolicy | None = None,
    org_mode: bool = False,
    permitted: Callable[[str], bool] | None = None,
) -> RepoRoot:
    """Run the eight checks for `cwd`; the first that fails raises GuardRefusal."""
    cwd = Path(cwd)
    # 1
    if _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        raise GuardRefusal(1, "Not a git repository", hint="cd into a git repository's root")
    top = Path(_git(cwd, "rev-parse", "--show-toplevel") or "")
    # 2
    if not _same(cwd, top):
        hint = f"Run from the repo root: cd {top}"
        raise GuardRefusal(2, f"{cwd} is not the repo root", hint=hint)
    # 3
    _not_a_run_tree_or_submodule(cwd, top)
    # 4
    if _is_root(top) or _same(top, home):
        raise GuardRefusal(
            4,
            f"Refusing to initialise {top}: it is a filesystem root or your home folder",
            hint="clone the repository into its own folder and run init there",
        )
    # 5
    origin_url = _git(top, "remote", "get-url", "origin")
    origin = normalise_remote(origin_url) if origin_url else None
    if org_mode:
        _allowed_by_policy(origin, policy)
    # 6
    committed = _git(top, "cat-file", "-p", f"HEAD:{CONFIG_REL}")
    config = _check_config(mode, committed, policy)
    # 7
    default_branch = _default_branch(top)
    if mode == "repo":
        _ready_for_shared_files(top, default_branch, org_mode, permitted)
    # 8
    nested = [p for p in _config_files(top) if p != CONFIG_REL]
    if nested:
        raise GuardRefusal(
            8,
            f"Found another .agentic at {nested[0].rsplit('/config.yaml', 1)[0]}",
            hint="a repository has one .agentic/ at its root; remove or merge the other one",
        )
    return RepoRoot(
        path=top,
        origin=origin,
        default_branch=default_branch,
        config=config,
        empty=is_empty(top),
    )


def normalise_remote(url: str) -> str:
    """`host/owner/repo`, lower case, from an https, ssh or scp-style git URL."""
    text = url.strip()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.IGNORECASE)  # scheme
    text = re.sub(r"^[^@/]*@", "", text)  # user@
    if re.match(r"^[^/:]+:(?!\d+/)", text):  # scp form host:owner/repo
        text = text.replace(":", "/", 1)
    text = re.sub(r":\d+/", "/", text, count=1)  # host:port/
    text = text.rstrip("/")
    text = text.removesuffix(".git")
    return text.lower()


# --- the checks ---


def _not_a_run_tree_or_submodule(cwd: Path, top: Path) -> None:
    parts = [part.lower() for part in Path(os.path.realpath(top)).parts]
    for marker in RUN_TREE_DIRS:
        for i in range(len(parts) - len(marker) + 1):
            if tuple(parts[i : i + len(marker)]) == marker:
                raise GuardRefusal(
                    3,
                    "This is a run worktree - run init in the main repo",
                    hint="cd to the repository the run belongs to",
                )
    superproject = _git(top, "rev-parse", "--show-superproject-working-tree")
    if superproject and not _listed_component(Path(superproject), top):
        raise GuardRefusal(
            3,
            "This is a submodule - run init in the main repo",
            hint="or list it under components: in the main repo's .agentic/config.yaml",
        )


def _listed_component(superproject: Path, top: Path) -> bool:
    path = superproject / CONFIG_REL
    try:
        config = load_repo_config(read_text(path), path)
        rel = Path(os.path.realpath(top)).relative_to(os.path.realpath(superproject))
    except (OSError, ValueError, YamlFileError, RepoConfigError):
        return False
    wanted = rel.as_posix().lower()
    return any(entry.replace("\\", "/").strip("/").lower() == wanted for entry in config.components)


def _allowed_by_policy(origin: str | None, policy: OrgPolicy | None) -> None:
    patterns = list(policy.allowed_repo_patterns) if policy is not None else []
    hint = "ask your team lead"
    if not patterns:
        raise GuardRefusal(
            5, "Repo not approved for agentic use: org policy allows no repos", hint=hint
        )
    if origin is None:
        raise GuardRefusal(5, "Repo not approved for agentic use: it has no origin remote", hint)
    if not any(fnmatch.fnmatchcase(origin, normalise_remote(p)) for p in patterns):
        raise GuardRefusal(5, f"Repo not approved for agentic use ({origin})", hint=hint)


def _check_config(mode: Mode, committed: str, policy: OrgPolicy | None) -> RepoConfig | None:
    if mode == "repo":
        if committed:
            raise GuardRefusal(
                6,
                f"This repo is already set up ({CONFIG_REL} is committed)",
                hint=f"change {CONFIG_REL} by a pull request; developers run `agentic init`",
            )
        return None
    if not committed:
        raise GuardRefusal(
            6, "Repo not initialised - run `agentic init --repo` (or ask your team lead)"
        )
    try:
        config = load_repo_config(committed)
    except RepoConfigError as error:
        raise GuardRefusal(6, str(error), hint=f"fix {CONFIG_REL} by a pull request") from error
    known = sorted(policy.classifications) if policy is not None else []
    if config.classification not in known:
        raise GuardRefusal(
            6,
            f"{CONFIG_REL}: classification '{config.classification}' is not defined in "
            f"org policy (known: {', '.join(known) or 'none'})",
            hint=f"fix {CONFIG_REL} by a pull request",
        )
    return config


def _ready_for_shared_files(
    top: Path, default_branch: str | None, org_mode: bool, permitted: Callable[[str], bool] | None
) -> None:
    if org_mode and (permitted is None or not permitted(PERMISSION)):
        why = "you do not hold" if permitted is not None else "this CLI cannot yet check"
        raise GuardRefusal(
            7,
            f"init --repo needs the permission {PERMISSION} for this team, and {why} it",
            hint="ask your team lead to run it, or use --standalone for a personal repo",
        )
    if _git(top, "status", "--porcelain"):
        raise GuardRefusal(
            7, "The working tree has uncommitted changes", hint="commit or stash them first"
        )
    current = _git(top, "symbolic-ref", "--short", "-q", "HEAD")
    if default_branch is None or current != default_branch:
        raise GuardRefusal(
            7,
            f"init --repo runs on the default branch ({default_branch or 'main or master'}), "
            f"not {current or 'a detached HEAD'}",
            hint=f"git switch {default_branch or 'main'}",
        )


def _default_branch(top: Path) -> str | None:
    remote_head = _git(top, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if remote_head:
        return remote_head.split("/", 1)[-1]
    for name in ("main", "master"):
        if _git(top, "rev-parse", "--verify", "-q", f"refs/heads/{name}"):
            return name
    if is_empty(top):  # `git init` and nothing since: the branch being started (M1-70)
        return _git(top, "symbolic-ref", "--short", "-q", "HEAD") or None
    return None


def is_empty(top: Path) -> bool:
    """A repository with no commit yet (its HEAD is unborn)."""
    return not _git(top, "rev-parse", "--verify", "-q", "HEAD")


def _config_files(top: Path) -> list[str]:
    found = _git(
        top,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        f":(glob){CONFIG_REL}",
        f":(glob)**/{CONFIG_REL}",
    )
    return sorted(set(found.splitlines()))


# --- helpers ---


def _git(where: Path, *args: str) -> str:
    """Git's output, stripped; "" when git fails or is missing (the checks treat it as
    the condition being absent)."""
    try:
        done = subprocess.run(
            [*_GIT, "-C", str(where), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _is_root(path: Path) -> bool:
    real = Path(os.path.realpath(path))
    return real.parent == real
