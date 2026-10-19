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

Components come from the repository's Zyroflo profile (`.sdlc/profile.yml`);
a repository without one is a single component.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tools.sdlc import policy, state
from tools.sdlc import worktree as zyroflo


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
            raise WorktreeError(f"run {self.run_id} is not open - open its ledger before "
                                f"making its trees")  # fmt: skip
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
    return None if components is None else list(components)


def _call(function, *args):
    try:
        return function(*args)
    except (zyroflo.WorktreeError, policy.PolicyError) as error:
        raise WorktreeError(str(error)) from error


def _tree(name: str, row: dict) -> Tree:
    return Tree(component=name, path=Path(row["home"]), branch=row.get("branch", ""),
                base=row.get("base", ""), mode=row["mode"])  # fmt: skip


def _live(record: dict) -> dict[str, Tree]:
    return {name: _tree(name, row) for name, row in record.get("components", {}).items()
            if not row.get("removed_at")}  # fmt: skip
