"""Worktree adapter over Zyroflo's run trees (M1-09; D34: reuse, never rewrite).

A run never works in the developer's checkout: it gets its own branch and
linked git worktree per component (`zyroflo.sdlc.worktree`). Zyroflo does the
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

A component Zyroflo makes in place (`vcs.deny_raw_git`) has no tree. A run of that one
component works in its own checkout when its commit-command override is accepted (M2-61,
`sdlc_commons.commit_override`): its checkpoints and rollbacks go through the component's
own commands, and git only reads it.

Components come from the repository's Zyroflo profile (`.sdlc/profile.yml`);
a repository without one is a single component.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from zyroflo.sdlc import policy, state
from zyroflo.sdlc import worktree as zyroflo

from sdlc_commons.ledger import run_id_refusal

_LOG = logging.getLogger("sdlc_commons.worktrees")

#: Untracked files a checkpoint never commits: what running a repository's tests or
#: linters leaves in the tree (bytecode, tool caches, coverage data, installed packages).
#: A repository that ignores them in .gitignore is unaffected; one that does not would
#: otherwise get them on the run branch and in its pull request (M1-68). The checkpoint
#: then deletes them (the next run regenerates them), as a fresh attempt already does.
#: Tracked files under these paths are still committed. Git glob pathspecs, relative to
#: the tree. Org policy adds to them (`org.yaml` `checkpoint_exclude`), never removes
#: one; each path left out is logged (DF-315, DF-317).
GENERATED: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/*.py[co]",  # not .pyd: a native extension is work
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


#: Where a run keeps the commit-command override its first `prepare` accepted (M2-61): in the
#: run folder, which agents never write, so later steps use these commands and no others.
OVERRIDE_FILE = "vcs-override.json"


@dataclass(frozen=True)
class InPlace:
    """An in-place component's commit-command override (M2-61), as accepted for one run:
    argv templates with whole-word placeholders; `push` is () when none is configured."""

    component: str
    checkout: Path  # the component's own checkout: agents work here, commands run here
    base: str  # the commit the run started from (Zyroflo's record)
    commit: tuple[str, ...]
    rollback: tuple[str, ...]
    push: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "checkout": str(self.checkout),
            "base": self.base,
            "commit": list(self.commit),
            "rollback": list(self.rollback),
            "push": list(self.push),
        }


class Override(Protocol):
    """The commit-command override (`sdlc_commons.commit_override.CommitOverride`)."""

    def resolve(self, trees: Mapping[str, "Tree"]) -> dict[str, InPlace]: ...

    def commit(self, cmds: InPlace, message: str) -> None: ...

    def rollback(self, cmds: InPlace, commit: str) -> None: ...


def kept_override(target: Path, run_id: str) -> dict[str, InPlace] | None:
    """The override the run accepted, by component; None when it accepted none (or the
    record cannot be read: then nothing in place is trusted)."""
    try:
        path = state.runs_dir(Path(target)) / run_id / OVERRIDE_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            name: InPlace(
                component=name,
                checkout=Path(row["checkout"]),
                base=str(row["base"]),
                commit=tuple(str(w) for w in row["commit"]),
                rollback=tuple(str(w) for w in row["rollback"]),
                push=tuple(str(w) for w in row.get("push", ())),
            )
            for name, row in data["components"].items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def keep_override(target: Path, run_id: str, accepted: Mapping[str, InPlace]) -> None:
    path = state.runs_dir(Path(target)) / run_id / OVERRIDE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"components": {name: cmds.as_json() for name, cmds in accepted.items()}}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def override_homes(target: Path, run_id: str) -> list[Path]:
    """The checkouts of the run's in-place components whose override it accepted: where its
    agents may work besides its trees (`Guard`)."""
    return [cmds.checkout for cmds in (kept_override(target, run_id) or {}).values()]


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

    def root(self) -> Path:
        """The folder holding this run's trees: `<worktrees_base>/<run-id>`."""
        return _trees_root(self.target, self.run_id)


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

    def __init__(
        self,
        target: Path,
        run_id: str,
        *,
        exclude: Sequence[str] = (),
        override: "Override | None" = None,
    ) -> None:
        self.worktrees = RunWorktrees(target, run_id)
        self.target, self.run_id = self.worktrees.target, run_id
        # the framework's exclusions plus the org's (`exclude`): only ever more
        self.generated = tuple(dict.fromkeys((*GENERATED, *exclude)))
        # the commit-command override of in-place components (M2-61); None: none
        self.override = override

    def prepare(self) -> Path:
        """Make the run's trees (harmless when they exist) and return where agents
        work: a single component's own tree, or the folder holding every tree - or, for a
        run of one in-place component whose commit-command override is accepted, that
        component's own checkout (M2-61)."""
        trees = self.worktrees.create()
        if in_place := sorted(name for name, tree in trees.items() if tree.mode != "worktree"):
            names = ", ".join(in_place)
            if len(trees) > 1:
                raise WorktreeError(
                    f"component(s) {names} work in place (vcs.deny_raw_git) in a run of "
                    f"{len(trees)} components: code steps on such a run need every component "
                    f"to have a tree, or to be its run's only component"
                )
            if self.override is None:
                raise WorktreeError(
                    f"component(s) {names} cannot get a tree (vcs.deny_raw_git): code steps "
                    f"need one to be checkpointed and rolled back, or the commit-command "
                    f"override - set vcs.commit_command in .sdlc/profile.yml and "
                    f"rollback_commands in .agentic/config.yaml, and allow both in org policy "
                    f"(commit_override.allow)"
                )
            accepted = self.override.resolve(trees)
            return next(iter(accepted.values())).checkout
        if len(trees) == 1:
            return next(iter(trees.values())).path
        return _trees_root(self.target, self.run_id)

    def begin(self, *, fresh: bool) -> dict[str, str]:
        """Where a step starts, by component. A fresh attempt first discards what an
        earlier, unfinished attempt left (tracked edits, untracked files and any commit an
        agent made in the tree: it returns to that attempt's base; files git ignores, such
        as `.venv/`, are kept). The step's base is kept in the run folder until the step
        is checkpointed or rolled back, so `changed` and a retry measure from it - in
        another process too (review F1)."""
        kept = self._kept_base()
        heads = {}
        for name, path in self._trees().items():
            if fresh:
                _git(path, "reset", "-q", "--hard", kept.get(name, "HEAD"))
                _git(path, "clean", "-q", "-ffd")  # -ff: nested repositories too (review I4)
            heads[name] = _git(path, "rev-parse", "HEAD")
        for name, cmds in self._in_place().items():
            # in place, the rollback command discards; only when there is something to
            # discard, so a clean start runs no command
            head = _git(cmds.checkout, "rev-parse", "HEAD")
            start = kept.get(name, head)
            if fresh and (start != head or _git(cmds.checkout, "status", "--porcelain")):
                self._overridden().rollback(cmds, start)
                head = self._at(cmds, start)
            heads[name] = head
        self._keep_base(heads if fresh or not kept else kept)
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
            generated = [f":(glob){g}" for g in self.generated]
            warned = set()
            for pattern in (g for g in self.generated if g not in GENERATED):
                # an org pattern broader than meant deletes the agent's work: say so loudly
                for rel in _new_files(path, f":(glob){pattern}") - warned:
                    warned.add(rel)
                    _LOG.warning(
                        "checkpoint of %s left out and deleted %s (org checkpoint_exclude %s)",
                        name,
                        rel,
                        pattern,
                    )
            for rel in sorted(_new_files(path, *generated) - warned):
                _LOG.info("checkpoint of %s left out %s (generated)", name, rel)
            excluded = (f":(exclude,glob){g}" for g in self.generated)
            _git(path, "add", "-A", "--", ".", *excluded)
            _git(path, "add", "-u")
            if _git(path, "diff", "--cached", "--name-only"):
                _git(path, *self._commit_config(path), "commit", "-q", "--no-verify", "-m", message)
            # and the caches left out go, so the tree is clean again: removing the run's
            # trees at closure refuses any uncommitted change (review F2); ignored files stay
            _git(path, "clean", "-q", "-fd", "--", *generated)
            heads[name] = _git(path, "rev-parse", "HEAD")
        for name, cmds in self._in_place().items():
            # in place, the component's own commit command commits (the framework's
            # exclusions are git pathspecs it cannot pass); it must leave nothing behind, or
            # that work is missing from the review and the pull request (review F1)
            if _git(cmds.checkout, "status", "--porcelain"):
                self._overridden().commit(cmds, message)
                if left := _left(cmds.checkout):
                    raise WorktreeError(
                        f"{name}: the commit command left changes it did not commit "
                        f"({left}) - it must commit every change, or the repository must "
                        f"ignore those files; check {cmds.checkout} by hand"
                    )
            heads[name] = _git(cmds.checkout, "rev-parse", "HEAD")
        self._keep_base(None)  # the next step starts from this checkpoint
        return heads

    def changed(self) -> list[Path]:
        """Every path a step changed since its base (`begin`; the trees' HEAD when none is
        kept), in every component: tracked files changed, added or deleted - staged, not
        staged or committed by the agent itself (review F1) - and untracked files git
        does not ignore: what this step would add to the run branch (M2-60: the post-step
        protected-path check of an agent CLI's writes)."""
        kept = self._kept_base()
        found: dict[Path, None] = {}
        for name, path in self._readable().items():
            base = kept.get(name, "HEAD")
            tracked = _git(path, "diff", "--name-only", "--no-renames", "-z", base, "--")
            untracked = _git(path, "ls-files", "-z", "--others", "--exclude-standard")
            for name in f"{tracked}\0{untracked}".split("\0"):
                if name:
                    found[path / name] = None
        return list(found)

    def rollback(self, to: Mapping[str, str]) -> None:
        """Put each named component back at `to[component]`, a commit of the run's own
        branch; uncommitted work is discarded."""
        trees, in_place = self._trees(), self._in_place()
        for name, commit in to.items():
            if name not in trees and name not in in_place:
                raise WorktreeError(f"rollback names {name!r}, which has no tree in this run")
            path = trees[name] if name in trees else in_place[name].checkout
            if _git(path, "merge-base", commit, "HEAD", check=False) != commit:
                raise WorktreeError(f"{commit} is not part of this run's branch in {name}")
        for name, commit in to.items():
            if name in in_place:  # its own rollback command, never a git write (M2-61)
                self._overridden().rollback(in_place[name], commit)
                self._at(in_place[name], commit)
                continue
            _git(trees[name], "reset", "-q", "--hard", commit)
            _git(trees[name], "clean", "-q", "-ffd")
        self._keep_base(None)  # the next step starts from where the trees were put

    def diff(self, base: Mapping[str, str], head: Mapping[str, str]) -> str:
        """The change from `base` to `head` in every component, as a plain unified diff
        with `a/` and `b/` prefixes whatever the repository's diff settings say (no
        external diff, no textconv, no colour). Each component's file names are its own
        (`diff --git a/x b/x`)."""
        parts = []
        for name, path in self._readable().items():
            start, end = base.get(name), head.get(name)
            if start and end and start != end:
                parts.append(_git(path, *_PLAIN_DIFF, start, end))
        return "\n".join(part for part in parts if part)

    def _trees(self) -> dict[str, Path]:
        return {
            name: tree.path
            for name, tree in self.worktrees.trees().items()
            if tree.mode == "worktree"
        }

    def _in_place(self) -> dict[str, InPlace]:
        """The run's in-place components whose override it accepted (M2-61)."""
        live = {n for n, t in self.worktrees.trees().items() if t.mode != "worktree"}
        kept = kept_override(self.target, self.run_id) or {}
        return {name: cmds for name, cmds in kept.items() if name in live}

    def _readable(self) -> dict[str, Path]:
        """Where git may READ the run's work: its trees and its in-place checkouts (Zyroflo:
        `deny_raw_git` forbids commit, push and worktree, not a read)."""
        return {**self._trees(), **{n: c.checkout for n, c in self._in_place().items()}}

    def _overridden(self) -> "Override":
        if self.override is None:
            raise WorktreeError(
                "this run works in place and was given no commit-command override to use"
            )
        return self.override

    def _at(self, cmds: InPlace, commit: str) -> str:
        """The checkout's HEAD after a rollback command, which must have put it at `commit`
        and discarded every uncommitted change (the rollback contract, review F2)."""
        head = _git(cmds.checkout, "rev-parse", "HEAD")
        if head != commit:
            raise WorktreeError(
                f"{cmds.component}: the rollback command left the checkout at {head}, not "
                f"{commit} - check {cmds.checkout} by hand"
            )
        if left := _left(cmds.checkout):
            raise WorktreeError(
                f"{cmds.component}: the rollback command left changes in the checkout "
                f"({left}) - it must discard uncommitted work, untracked files included; "
                f"check {cmds.checkout} by hand"
            )
        return head

    def _base_file(self) -> Path:
        """Where the base of the step in progress is kept: the run folder, outside every
        tree (an agent never writes it)."""
        return state.runs_dir(self.target) / self.run_id / "step-base.json"

    def _kept_base(self) -> dict[str, str]:
        """The base of the step in progress, by component; {} when no step is in progress
        (or the record cannot be read: then each tree's HEAD, as before)."""
        try:
            kept = json.loads(self._base_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(kept, dict):
            return {}
        return {k: v for k, v in kept.items() if isinstance(k, str) and isinstance(v, str)}

    def _keep_base(self, heads: Mapping[str, str] | None) -> None:
        where = self._base_file()
        if heads is None:
            where.unlink(missing_ok=True)
            return
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(dict(heads)), encoding="utf-8")

    def _commit_config(self, path: Path) -> list[str]:
        """No hooks (an empty hooks folder), no signing, and an identity only when the
        repository has none."""
        hooks = state.runs_dir(self.target) / self.run_id / "no-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        config = ["-c", f"core.hooksPath={hooks.as_posix()}", "-c", "commit.gpgsign=false"]
        if not _git(path, "config", "user.email", check=False):
            config += ["-c", "user.name=agentic", "-c", "user.email=agentic@localhost"]
        return config


#: `git diff` as Zyroflo's budget reads it, whatever the repository configures or the
#: agent's `.gitattributes` says (`--text`: a `-diff` or `binary` file still counts).
_PLAIN_DIFF = (
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--text",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def change_since_base(target: Path, run_id: str) -> list[tuple[str, Path, str]]:
    """Each live tree's change so far: (component, tree, plain unified diff from the commit
    the tree was made from to its HEAD) - what a reviewer of the run reads (M2-48)."""
    found = []
    accepted = kept_override(target, run_id) or {}  # in place, with its override (M2-61)
    for name, tree in sorted(RunWorktrees(target, run_id).trees().items()):
        if (tree.mode != "worktree" and name not in accepted) or not tree.base:
            continue
        found.append((name, tree.path, _git(tree.path, *_PLAIN_DIFF, tree.base, "HEAD")))
    return found


def _git(where: Path, *args: str, check: bool = True) -> str:
    """A checkpoint git call: never runs an fsmonitor command the tree's or the
    repository's config names (agent-reachable without a sandbox, review I5)."""
    return worktree_git(where, "-c", "core.fsmonitor=false", *args, check=check)


def _left(checkout: Path, shown: int = 5) -> str:
    """What is uncommitted in an in-place checkout (files git ignores aside), a few paths
    named; "" when it is clean."""
    if not _git(checkout, "status", "--porcelain"):
        return ""
    tracked = _git(checkout, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")
    untracked = _git(checkout, "ls-files", "-z", "--others", "--exclude-standard")
    paths = list(dict.fromkeys(p for p in f"{tracked}\0{untracked}".split("\0") if p))
    more = f" and {len(paths) - shown} more" if len(paths) > shown else ""
    return ", ".join(paths[:shown]) + more or "see git status"


def _new_files(tree: Path, *pathspecs: str) -> set[str]:
    """Untracked files in `tree` git does not ignore that match `pathspecs`."""
    listing = _git(tree, "ls-files", "-z", "--others", "--exclude-standard", "--", *pathspecs)
    return set(filter(None, listing.split("\0")))


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


def worktrees_base(target: Path) -> Path:
    """The folder holding every run's trees: `<target>/<worktrees_root>` from the
    repository's Zyroflo profile (`.sdlc-worktrees` by default). `agentic gc` and the disk
    cap of `agentic status` measure and prune it (M3-08)."""
    try:
        repo = policy.load(Path(target))
    except policy.PolicyError as error:
        raise WorktreeError(str(error)) from error
    return Path(target) / repo["worktrees_root"]


def _trees_root(target: Path, run_id: str) -> Path:
    """The folder holding a run's trees (the same as `Guard.trees_root`)."""
    return worktrees_base(target) / run_id
