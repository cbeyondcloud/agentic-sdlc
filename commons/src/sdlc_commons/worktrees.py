"""Worktree adapter over Zyroflo's run trees (M1-09; D34: reuse, never rewrite).

A run never works in the developer's checkout: it gets its own branch and
linked git worktree per component (`tools.sdlc.worktree`). Zyroflo does the
work and keeps the guarantees; this module gives the framework a small,
typed surface over it:

  * plan    - the trees `create` would make, without making any (every
              refusal collected first, Zyroflo's rule);
  * create  - all or nothing: a failure part-way takes back the trees already
              made (Zyroflo's unwind), so a run never holds half its components;
  * trees   - the run's live trees, from Zyroflo's record in the run folder;
  * remove  - at closure; refuses to lose uncommitted work unless forced.

`StepCheckpoints` adds the per-step view the engine needs (M1-30, spec §9.2):
each agent step starts from the previous step's commit, ends in a checkpoint
commit on the run branch (the step's reviewable diff), and a rejection rolls
the tree back to the step's base. Checkpoint commits run OUTSIDE the sandbox
on files an agent wrote, so they never run the repository's git hooks.

Components come from the repository's Zyroflo profile (`.sdlc/profile.yml`);
a repository without one is a single component.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tools.sdlc import policy, state
from tools.sdlc import worktree as zyroflo

from sdlc_commons.ledger import run_id_refusal

#: Untracked files a checkpoint never commits: what running a repository's tests or
#: linters leaves in the tree (bytecode, tool caches, coverage data, installed packages).
#: A repository that ignores them in .gitignore is unaffected; one that does not would
#: otherwise get them on the run branch and in its pull request (M1-68). Tracked files
#: under these paths are still committed. Git glob pathspecs, relative to the tree.
GENERATED: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/*.py[cod]",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.hypothesis/**",
    "**/.tox/**",
    "**/.nox/**",
    "**/.coverage",
    "**/.coverage.*",
    "**/node_modules/**",
)


class WorktreeError(Exception):
    """A worktree operation was refused or failed; the message says what is left on disk."""


@dataclass(frozen=True)
class Tree:
    """Where one component of a run is worked on."""

    component: str
    path: Path  # the component's folder inside the run's tree - agents work here
    branch: str  # e.g. "sdlc/<run-id>"
    base: str  # the commit the tree was made from; every later diff is against it
    mode: str  # "worktree", or "in-place" for a component Zyroflo cannot give a tree


class RunWorktrees:
    """The trees of one run."""

    def __init__(self, target: Path, run_id: str) -> None:
        if why := run_id_refusal(run_id):
            raise WorktreeError(why)
        self.target = Path(target)
        self.run_id = run_id

    def plan(self, components: Sequence[str] | None = None) -> dict[str, Tree]:
        """The trees `create` would make, by component. Raises with EVERY problem found."""
        result = _call(zyroflo.plan, self.target, self.run_id, _names(components))
        problems = [f"{step['name']}: {p}" for step in result["steps"] for p in step["problems"]]
        if problems:
            raise WorktreeError("; ".join(problems))
        return {step["name"]: _tree(step["name"], step) for step in result["steps"]}

    def create(self, components: Sequence[str] | None = None) -> dict[str, Tree]:
        """Make the run's branches and trees (all components, or the named ones).
        Making them again is harmless: trees the run already has are kept."""
        if not (state.runs_dir(self.target) / self.run_id).is_dir():
            raise WorktreeError(
                f"run {self.run_id} is not open - open its ledger before making its trees"
            )
        record = _call(zyroflo.create, self.target, self.run_id, _names(components))
        return _live(record)

    def trees(self) -> dict[str, Tree]:
        """The run's live trees (removed ones are left out)."""
        return _live(_call(zyroflo.load_record, self.target, self.run_id))

    def remove(self, force: bool = False) -> list[str]:
        """Take the run's trees away; returns what was done. Uncommitted work is
        refused unless `force` (an agent's unfinished edit is the only copy of it)."""
        return _call(zyroflo.remove, self.target, self.run_id, force)


def _names(components: Sequence[str] | None) -> list[str] | None:
    """A list of component names - never one name read letter by letter."""
    if isinstance(components, str):
        raise WorktreeError(f"components is a list of names, not one string ({components!r})")
    return None if components is None else list(components)


def _call(function, *args):
    try:
        return function(*args)
    except (zyroflo.WorktreeError, policy.PolicyError, ValueError, OSError) as error:
        raise WorktreeError(str(error)) from error


def _tree(name: str, row: dict) -> Tree:
    # An in-place component has no tree; before it is made, its checkout is `where`.
    home = row.get("home") or row.get("where")
    return Tree(
        component=name,
        path=Path(home),
        branch=row.get("branch") or "",
        base=row.get("base") or "",
        mode=row["mode"],
    )


def _live(record: dict) -> dict[str, Tree]:
    return {
        name: _tree(name, row)
        for name, row in record.get("components", {}).items()
        if not row.get("removed_at")
    }


class StepCheckpoints:
    """Per-step checkpoints on one run's trees (all components at once)."""

    def __init__(self, target: Path, run_id: str) -> None:
        self.worktrees = RunWorktrees(target, run_id)
        self.target, self.run_id = self.worktrees.target, run_id

    def prepare(self) -> Path:
        """Make the run's trees (harmless when they exist) and return where agents
        work: a single component's own tree, or the folder holding every tree."""
        trees = self.worktrees.create()
        if in_place := sorted(name for name, tree in trees.items() if tree.mode != "worktree"):
            raise WorktreeError(
                f"component(s) {', '.join(in_place)} cannot get a tree (vcs.deny_raw_git): code "
                f"steps need one to be checkpointed and rolled back"
            )
        if len(trees) == 1:
            return next(iter(trees.values())).path
        return _trees_root(self.target, self.run_id)

    def begin(self, *, fresh: bool) -> dict[str, str]:
        """Where a step starts, by component. A fresh attempt first discards what an
        earlier, unfinished attempt left (tracked edits and untracked files; files
        git ignores, such as `.venv/`, are kept)."""
        heads = {}
        for name, path in self._trees().items():
            if fresh:
                _git(path, "reset", "-q", "--hard", "HEAD")
                _git(path, "clean", "-q", "-ffd")  # -ff: nested repositories too (review I4)
            heads[name] = _git(path, "rev-parse", "HEAD")
        return heads

    def finish(self, message: str) -> dict[str, str]:
        """Commit the step's work in every tree that changed; returns each HEAD."""
        heads = {}
        for name, path in self._trees().items():
            if nested := _nested_repositories(path):
                raise WorktreeError(
                    f"{name}: an agent left a git repository inside the tree "
                    f"({', '.join(nested)}); it cannot be checkpointed - the next fresh "
                    f"attempt removes it"
                )
            # new files, minus what running the tests leaves behind; then every change to
            # a tracked file, wherever it is (a repository may vendor node_modules)
            _git(path, "add", "-A", "--", ".", *(f":(exclude,glob){g}" for g in GENERATED))
            _git(path, "add", "-u")
            if _git(path, "diff", "--cached", "--name-only"):
                _git(path, *self._commit_config(path), "commit", "-q", "--no-verify", "-m", message)
            heads[name] = _git(path, "rev-parse", "HEAD")
        return heads

    def rollback(self, to: Mapping[str, str]) -> None:
        """Put each named component back at `to[component]`, a commit of the run's own
        branch; uncommitted work is discarded."""
        trees = self._trees()
        for name, commit in to.items():
            if name not in trees:
                raise WorktreeError(f"rollback names {name!r}, which has no tree in this run")
            path = trees[name]
            if _git(path, "merge-base", commit, "HEAD", check=False) != commit:
                raise WorktreeError(f"{commit} is not part of this run's branch in {name}")
        for name, commit in to.items():
            _git(trees[name], "reset", "-q", "--hard", commit)
            _git(trees[name], "clean", "-q", "-ffd")

    def _trees(self) -> dict[str, Path]:
        return {
            name: tree.path
            for name, tree in self.worktrees.trees().items()
            if tree.mode == "worktree"
        }

    def _commit_config(self, path: Path) -> list[str]:
        """No hooks (an empty hooks folder), no signing, and an identity only when the
        repository has none."""
        hooks = state.runs_dir(self.target) / self.run_id / "no-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        config = ["-c", f"core.hooksPath={hooks.as_posix()}", "-c", "commit.gpgsign=false"]
        if not _git(path, "config", "user.email", check=False):
            config += ["-c", "user.name=agentic", "-c", "user.email=agentic@localhost"]
        return config


def _git(where: Path, *args: str, check: bool = True) -> str:
    """A checkpoint git call: never runs an fsmonitor command the tree's or the
    repository's config names (agent-reachable without a sandbox, review I5)."""
    return worktree_git(where, "-c", "core.fsmonitor=false", *args, check=check)


def _nested_repositories(tree: Path) -> list[str]:
    """Untracked folders in `tree` that hold a git repository of their own."""
    listing = _git(tree, "ls-files", "--others", "--exclude-standard", "--directory")
    found = []
    for entry in listing.splitlines():
        folder = tree / entry.rstrip("/")
        if entry.endswith("/") and folder.is_dir():
            if (folder / ".git").exists() or any(folder.rglob(".git")):
                found.append(entry.rstrip("/"))
    return found


def worktree_git(where: Path, *args: str, check: bool = True) -> str:
    """One git call through Zyroflo's helper, its failure as a WorktreeError."""
    try:
        return zyroflo.git(where, *args, check=check)
    except zyroflo.WorktreeError as error:
        raise WorktreeError(str(error)) from error


def _trees_root(target: Path, run_id: str) -> Path:
    """The folder holding a run's trees (the same as `Guard.trees_root`)."""
    try:
        repo = policy.load(Path(target))
    except policy.PolicyError as error:
        raise WorktreeError(str(error)) from error
    return Path(target) / repo["worktrees_root"] / run_id
