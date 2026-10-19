"""Guard adapter over Zyroflo's walls (M1-08; D31 deny lists; D34: reuse, never rewrite).

May an agent run this command, write these paths, hand over this diff, push
there? Every answer comes from Zyroflo's guard functions (`tools.sdlc.policy`):

  * command - Zyroflo's destructive defaults (`destructive`: git history and
              data loss, matched by token in every shell), plus the agent's
              resolved deny list (org + team + agent, D31), matched by the same
              tokenizer (`denied_command`);
  * writes  - confined to the run's worktree (`confinement`), never a path only
              Zyroflo's CLIs write (`protected`), never a path on the deny list;
  * diff    - small enough to review (`budget`); a caller may only tighten it;
  * push    - only to the run's own branch on an approved remote (`push_target`).

Each check returns the reasons it refuses as `Block`s; an empty list means allowed.
Deny lists only ever add to Zyroflo's built-in walls - they never replace them.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tools.sdlc import policy
from tools.sdlc.guard import scrub


class GuardError(Exception):
    """The guard could not be built (e.g. the repository's Zyroflo policy is invalid)."""


@dataclass(frozen=True)
class Block:
    """One reason an action is refused: which wall, and why."""

    wall: str  # destructive | denied-command | worktree | protected | denied-path | budget | push
    why: str


class Guard:
    """The walls one agent's actions are checked against, for one run."""

    def __init__(
        self,
        target: Path,
        run_id: str,
        *,
        deny_shell: Iterable[str] = (),
        deny_paths: Iterable[str] = (),
        max_files: int | None = None,
        max_lines: int | None = None,
    ) -> None:
        self.target = Path(target)
        self.run_id = run_id
        self.deny_shell = tuple(deny_shell)
        self.deny_paths = tuple(deny_paths)
        try:
            base = policy.load(self.target)  # the repo's policy, or Zyroflo's defaults
        except policy.PolicyError as error:
            raise GuardError(str(error)) from error
        # A new dict, never an edit of Zyroflo's: `load` returns its defaults' lists.
        self.policy = {
            **base,
            "max_files_per_batch": _tighter(base["max_files_per_batch"], max_files),
            "max_lines_per_batch": _tighter(base["max_lines_per_batch"], max_lines),
        }

    @classmethod
    def for_agent(cls, target: Path, run_id: str, agent: object, **budget: int) -> "Guard":
        """A guard for a resolved agent (its `deny_shell` / `deny_paths`, D31)."""
        return cls(target, run_id, deny_shell=agent.deny_shell, deny_paths=agent.deny_paths,
                   **budget)  # fmt: skip

    @property
    def worktree(self) -> Path:
        """The only folder this run's agents may write to (Zyroflo layout)."""
        return self.target / self.policy["worktrees_root"] / self.run_id

    # ── commands ──────────────────────────────────────────────────────────────

    def command(self, command: str) -> list[Block]:
        """Every reason this shell command is refused. Reasons never quote the
        command, so a credential typed in it does not reach a log."""
        blocks = [
            Block("destructive", scrub(reason.replace(repr(command), "the command", 1)))
            for reason in policy.destructive(self.policy, command)
        ]
        for rule in self.deny_shell:
            if _denies(rule, command):
                why = f"the command matches the deny rule {rule!r} (org, team or agent policy)"
                blocks.append(Block("denied-command", why))
        return blocks

    # ── writes ────────────────────────────────────────────────────────────────

    def writes(self, paths: Sequence[Path | str]) -> list[Block]:
        """Every reason writing these paths is refused."""
        blocks = [
            Block("worktree", r)
            for r in policy.confinement(self.policy, self.target, self.run_id, list(paths))
        ]
        blocks += [Block("protected", r)
                   for r in policy.protected(self.policy, self.target, list(paths))]  # fmt: skip
        for raw in paths:
            # Deny globs are written relative to the repository ("infra/prod/**"),
            # and agents write inside their worktree, so match from there.
            root = self.worktree if _inside(Path(raw), self.worktree) else self.target
            for glob in self.deny_paths:
                if policy.protected({"protected_paths": [glob]}, root, [raw]):
                    blocks.append(Block("denied-path", f"{_shown(Path(raw), root)}: matches the "
                                                       f"deny rule {glob!r} (paths)"))  # fmt: skip
        return blocks

    # ── diff and push ─────────────────────────────────────────────────────────

    def diff(self, diff_text: str) -> list[Block]:
        """Whether one batch is small enough to review (Zyroflo's budget)."""
        return [Block("budget", r) for r in policy.budget(self.policy, diff_text)]

    def push(self, remote: str, branch: str) -> list[Block]:
        """Whether this push goes to the run's own branch on an approved remote."""
        return [Block("push", r)
                for r in policy.push_target(self.policy, remote, branch, self.run_id)]  # fmt: skip


def _tighter(base: int, requested: int | None) -> int:
    """The smaller budget; Zyroflo reads 0 as "no limit"."""
    if requested is None:
        return base
    if requested < 1:
        raise GuardError(f"a diff budget must be at least 1, not {requested}")
    return requested if not base else min(base, requested)


# ── deny rules ────────────────────────────────────────────────────────────────

_WORDS = re.compile(r"[\s;&|()<>`'\"{}]+|\$\(")
_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")


def _denies(rule: str, command: str) -> bool:
    if policy.denied_command({"deny_commands": [rule]}, command):
        return True
    # Zyroflo looks THROUGH wrappers (sudo, env, nohup, …) to the command they
    # run, so a rule naming the wrapper itself can never match there. For those,
    # any use of the word refuses: a deny list may over-refuse, never under-refuse.
    parts = rule.split()
    if not parts or _zyroflo_matches_program(_program(parts[0])):
        return False
    words = [w for w in _WORDS.split(command) if w]
    return _program(parts[0]) in {_program(w) for w in words} and all(p in words for p in parts[1:])


@lru_cache(maxsize=256)
def _zyroflo_matches_program(program: str) -> bool:
    """Whether Zyroflo's tokenizer can see `program` as a program (not a wrapper)."""
    return bool(policy.denied_command({"deny_commands": [program]}, f"{program} probe"))


def _program(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _shown(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
