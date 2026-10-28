"""Where a command's inputs come from (M1-54): the repository, its config, the org policy
repo, the team and the person.

  * repository   - the nearest folder holding `.git` (a folder, or a file in a worktree);
  * repo config  - `.agentic/config.yaml` (committed): `classification`;
  * policy repo  - `AGENTIC_POLICY_REPO`; else, with `org` in the user config, the policy
                   cache `~/.agentic/policy/<org>/repo/` written by `agentic init` (the
                   config's `policy_repo` is then where init fetches it from, often a
                   URL); else the config's local `policy` or `policy_repo` folder; else
                   the policy `init --standalone [--policy X]` recorded for this repo in
                   `.agentic/local/init.json` (only a folder outside the repo; refused,
                   never replaced, when that folder is gone); else the
                   standalone `.agentic/local/policy/`, else `~/.agentic/policy/standalone/`;
                   its `policy/` folder is the org policy, its root the org layer of
                   agents and pipelines, `teams/<team>/` the team layer;
  * team         - `AGENTIC_TEAM`, else the user config's `team`;
  * person       - `AGENTIC_USER`, else the user config's `user`, else the repository's
                   `git config user.email`. Never invented: a decision or a run attributed
                   to nobody is refused.

Everything is read lazily, so `agentic runs` works without a policy and a broken
definition is reported by the command that needs it.
"""

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from agentic.definitions import DefinitionError, DefinitionSet
from agentic.policy import OrgPolicy, ResolveContext, load_policy
from agentic.policy.repos import normalise_remote
from agentic.yamlio import YamlFileError, load_yaml, read_text
from agentic_cli.config import agentic_home, local_policy_setting, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.policy_source import check_control_plane_cache, control_plane_stamp
from agentic_cli.standalone import STANDALONE_DIR

REPO_CONFIG = Path(".agentic") / "config.yaml"
RUNS = Path(".agentic") / "local" / "runs"
STANDALONE_POLICY = Path(".agentic") / "local" / "policy"
INIT_STATE = Path(".agentic") / "local" / "init.json"  # written by `agentic init`


def utc_today() -> date:
    return datetime.now(UTC).date()


def find_repo(start: Path) -> Path:
    """The repository holding `start`: the nearest folder with a `.git` entry."""
    here = Path(start).resolve()
    for folder in (here, *here.parents):
        if (folder / ".git").exists():
            return folder
    raise CliError(
        f"{here} is not inside a git repository",
        ExitCode.REFUSED,
        hint="cd into the repository the run is for",
    )


def repo_config(repo: Path) -> dict[str, Any]:
    path = repo / REPO_CONFIG
    if not path.is_file():
        return {}
    try:
        data, _ = load_yaml(read_text(path))
    except YamlFileError as error:
        raise CliError(f"{path}: {error}", ExitCode.INVALID) from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CliError(f"{path}: expected a mapping of settings", ExitCode.INVALID)
    return data


@dataclass
class Workspace:
    repo: Path
    home: Path
    env: Mapping[str, str]
    settings: dict[str, Any]  # the user config
    config: dict[str, Any]  # the repo config
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(
        cls,
        cwd: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> "Workspace":
        env = os.environ if env is None else env
        repo = find_repo(cwd or Path.cwd())
        home = home if home is not None else agentic_home(env)
        return cls(repo, home, env, user_config(home), repo_config(repo))

    @property
    def runs_root(self) -> Path:
        return self.repo / RUNS

    @cached_property
    def policy_repo(self) -> Path | None:
        return self.policy_repo_with(recorded_policy(self.repo))

    def policy_repo_with(self, recorded: Path | None) -> Path | None:
        """The policy repo, given the policy `agentic init --standalone` recorded for this
        repository (`recorded_policy`): used after the explicit settings, and only when it
        is an absolute folder outside the repository, so a repo cannot point the policy
        at its own files."""
        org = self.settings.get("org")
        given = self.env.get("AGENTIC_POLICY_REPO", "").strip()
        if not given and not org:
            given = local_policy_setting(self.settings) or ""
        if given:
            return Path(given).expanduser()
        if org:
            cached = self.home / "policy" / str(org)
            if (cached / "repo").is_dir():  # where `agentic init` caches the policy repo
                return cached / "repo"
            if cached.is_dir():
                return cached
            # an org user never gets a personal policy (no org governance) in its place,
            # silently: the org policy is reported missing (review F3)
            return cached / "repo"
        if recorded is not None and (root := recorded_root(recorded, self.repo)):
            if not (root / "policy").is_dir():
                # never the template in its place, silently: the person chose this one
                raise CliError(
                    f"the policy recorded for this repository is gone: {root} has no "
                    "policy/ folder",
                    ExitCode.REFUSED,
                    hint="restore it (or reconnect its drive), or choose a policy with "
                    "`agentic init --standalone --policy <folder>`",
                )
            return root
        if (self.repo / STANDALONE_POLICY).is_dir() and not _tracked(self.repo, STANDALONE_POLICY):
            return self.repo / STANDALONE_POLICY  # a committed one came with a clone (DF-353)
        if (self.home / STANDALONE_DIR).is_dir():  # `agentic init --standalone` (M1-62)
            return self.home / STANDALONE_DIR
        return None

    @property
    def team(self) -> str | None:
        return self.env.get("AGENTIC_TEAM", "").strip() or self.settings.get("team") or None

    @property
    def classification(self) -> str | None:
        return self.config.get("classification") or None

    def context(self) -> ResolveContext:
        return ResolveContext(team=self.team, repo_classification=self.classification)

    def _policy_root(self) -> Path:
        root = self.policy_repo
        if root is None or not root.is_dir():
            where = f" ({root} does not exist)" if root is not None else ""
            raise CliError(
                f"no org policy found{where}",
                ExitCode.REFUSED,
                hint="run `agentic init` (or set AGENTIC_POLICY_REPO to a policy repo checkout)",
            )
        return root

    def policy(self) -> OrgPolicy:
        """The org policy. A policy the control plane served (`agentic init` in org mode
        with one) is refused when edited locally or of another tenant than the one the
        user is signed in to, and keeps the version the control plane validated."""
        if "policy" not in self._cache:
            root = self._policy_root()
            stamp = control_plane_stamp(root) if self._is_org_cache(root) else None
            if stamp is not None:
                check_control_plane_cache(root, stamp, self.home)
            try:
                self._cache["policy"] = load_policy(
                    root / "policy", version=stamp.get("version") if stamp else None
                )
            except DefinitionError as error:
                raise CliError(str(error), ExitCode.INVALID) from error
        return self._cache["policy"]

    def org_policy_cache(self) -> bool:
        """Whether the policy is the org's, as `agentic init` cached it (from the control
        plane or the org's policy repo): something other than this person moves it on."""
        root = self.policy_repo
        return root is not None and self._is_org_cache(root)

    def _is_org_cache(self, root: Path) -> bool:
        org = self.settings.get("org")
        return bool(org) and root == self.home / "policy" / str(org) / "repo"

    @cached_property
    def repo_key(self) -> str | None:
        """This repository as `host/owner/name` (its origin), or None without one: which
        overlay applies (M2-23), and the repo the control plane checks."""
        from agentic_cli.repo_guard import origin_url

        remote = origin_url(self.repo)
        return normalise_remote(remote) if remote else None

    def policy_path(self, path: Path) -> str:
        """`path` as its POSIX path in the policy repo (the name the control plane and the
        policy repo's reviewers know it by), or its file name outside it."""
        root = self.policy_repo
        try:
            return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        except (TypeError, ValueError, OSError):
            return Path(path).name

    def today(self) -> date:
        """Today (UTC), for deprecations (M2-15); tests replace `utc_today`."""
        return utc_today()

    def definitions(self) -> DefinitionSet:
        """The agents and pipelines this run gets: each at the version the team gets (its
        pin, the rollout), with the team's overlay for this repository applied (M2-15,
        M2-23; the order is in `agentic.definitions.versions`)."""
        if "definitions" not in self._cache:
            root, team = self._policy_root(), self.team
            team_layer = root / "teams" / team if team else None
            policy = self.policy()
            try:
                self._cache["definitions"] = DefinitionSet.discover(
                    org=root,
                    team=team_layer if team_layer is not None and team_layer.is_dir() else None,
                    repo=self.repo / ".agentic",
                    allow_repo_pipelines=policy.allow_repo_pipelines,
                    rules=policy.rules(team=team, repo=self.repo_key, today=self.today()),
                )
            except DefinitionError as error:
                raise CliError(str(error), ExitCode.INVALID) from error
        return self._cache["definitions"]

    def user(self) -> str:
        if "user" not in self._cache:
            found = self.env.get("AGENTIC_USER", "").strip() or str(self.settings.get("user") or "")
            self._cache["user"] = found or _git_email(self.repo)
        if not self._cache["user"]:
            raise CliError(
                "who you are is not known: no AGENTIC_USER, no `user` in the user config "
                "and no git identity in this repository",
                ExitCode.REFUSED,
                hint="set git config user.email (or AGENTIC_USER)",
            )
        return self._cache["user"]


def recorded_policy(repo: Path) -> Path | None:
    """The local policy `agentic init --standalone` used for `repo`, from the init state
    it saved (`.agentic/local/init.json`); None for an org init or when there is none.

    A committed init.json is ignored (`.agentic/local/` is git-ignored, so it came with a
    clone, not from this person's init), and a record naming a network share (a UNC path)
    is refused before anything touches it: on Windows that alone sends the user's
    credentials to the server (DF-353)."""
    try:
        state = json.loads((repo / INIT_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("mode") != "standalone":
        return None
    source = state.get("policy_source")
    if not isinstance(source, str) or not source.strip():
        return None
    if _tracked(repo, INIT_STATE):
        return None
    if source.strip().startswith(("\\\\", "//")):
        raise CliError(
            f"the policy recorded for this repository is on a network share ({source.strip()}); "
            "a recorded policy is never read from one",
            ExitCode.REFUSED,
            hint="copy the policy to a local folder and run `agentic init --standalone "
            "--policy <folder>`, or set AGENTIC_POLICY_REPO",
        )
    return Path(source.strip()).expanduser()


def _tracked(repo: Path, rel: Path) -> bool:
    """Whether git tracks `rel` (a file, or anything under a folder) in `repo`."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--", rel.as_posix()],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and bool(done.stdout.strip(b"\0"))


def recorded_root(path: Path, repo: Path) -> Path | None:
    """The policy repo root a recorded `path` names (the root, or its `policy/` folder),
    when it is absolute and outside `repo`; None otherwise (a repo never chooses its own
    policy). The folder may be gone since: callers check."""
    if not path.is_absolute():
        return None
    root = path.parent if path.name == "policy" and not (path / "policy").is_dir() else path
    try:
        resolved, top = root.resolve(), repo.resolve()
    except OSError:
        return root  # cannot be followed: reported as gone, never silently replaced
    if resolved == top or top in resolved.parents:
        return None
    return root


def _git_email(repo: Path) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "config", "user.email"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""
