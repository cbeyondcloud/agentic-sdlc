"""Guard adapter over Zyroflo's walls (M1-08; D31 deny lists; D34: reuse, never rewrite).

May an agent run this command, write these paths, hand over this diff, push
there? Every match is made by Zyroflo's guard functions (`tools.sdlc.policy`);
this module decides WHAT is asked of them:

  * command - Zyroflo's destructive defaults (`destructive`), the agent's
              resolved deny list (org + team + agent, D31) and the framework's
              own floor (`FRAMEWORK_DENY_SHELL`), each asked of the command as
              typed AND of the spellings that hide a program from a tokenizer
              (quotes inside a word, a quoted Windows path, a leading redirect,
              `then`/`do`/`!`). Any hit refuses: a deny list may over-refuse,
              never under-refuse;
  * writes  - confined to the run's trees (`confinement`), never a path only
              Zyroflo's CLIs write (`protected`), never a framework-protected
              file or a path on the deny list - matched relative to the tree
              (component) the path is in;
  * diff    - small enough to review (`budget`); a caller may only tighten it;
  * push    - only to the run's own branch on an approved remote (`push_target`).

Zyroflo's defaults are a FLOOR: a repository's own policy file may add to its
lists, never remove from them.

**What a deny list cannot do.** Text handed to an interpreter (`… | bash`,
`$VAR`, `python -c`, `docker run`, `ssh`) is not visible to any command matcher.
The allow-list (M1-31: an unknown program is refused) and the OS sandbox
(M1-32/33) are the controls for those; this guard is defence in depth.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tools.sdlc import policy
from tools.sdlc.guard import scrub

from sdlc_commons.worktrees import RunWorktrees, WorktreeError

#: Refused for every agent, whatever org, team or agent policy says.
FRAMEWORK_DENY_SHELL = (
    "git -c",  # `git -c alias.x='!cmd'` / `core.sshCommand=…` run arbitrary commands
    "git --config-env",
    "git config",
    "git push",  # publishing is the framework's job (M1-58), never an agent's
    "git remote",
    "eval",  # text run as a command cannot be checked
    "iex",
    "invoke-expression",
    "busybox",
    "powershell -e|-ec|-enc|-encodedcommand",  # base64 hides the command
    "pwsh -e|-ec|-enc|-encodedcommand",
)
#: Files inside a tree that steer agents, hooks or CI: never written by an agent.
FRAMEWORK_PROTECTED = (
    "**/.git", "**/.git/**", "**/.github/workflows/**", "**/.claude/**", "**/CLAUDE.md",
    "**/AGENTS.md", "**/.agentic/**", "**/.husky/**",
)  # fmt: skip
#: Zyroflo lists a repository's policy may extend but never shorten.
_FLOOR_LISTS = ("deny_git", "deny_commands", "protected_paths")


class GuardError(Exception):
    """The guard could not be built (an invalid policy, or rules that would deny nothing)."""


@dataclass(frozen=True)
class Block:
    """One reason an action is refused: which wall, and why."""

    #: destructive | denied-command | framework | worktree | protected | denied-path |
    #: budget | push
    wall: str
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
        self.deny_shell = _rules("deny_shell", deny_shell)
        self.deny_paths = _rules("deny_paths", deny_paths)
        try:
            repo = policy.load(self.target)  # the repo's policy, or Zyroflo's defaults
        except policy.PolicyError as error:
            raise GuardError(str(error)) from error
        # A new dict, never an edit of Zyroflo's: `load` returns its defaults' lists.
        self.policy = {**repo}
        for key in _FLOOR_LISTS:
            self.policy[key] = list(dict.fromkeys([*policy.DEFAULTS[key], *repo[key]]))
        for key, requested in (("max_files_per_batch", max_files),
                               ("max_lines_per_batch", max_lines)):  # fmt: skip
            floor = _tighter(policy.DEFAULTS[key], repo[key] or None)
            self.policy[key] = _tighter(floor, _budget(key, requested))

    @classmethod
    def for_agent(cls, target: Path, run_id: str, agent: object, **budget: int) -> "Guard":
        """A guard for a resolved agent (its `deny_shell` / `deny_paths`, D31)."""
        return cls(target, run_id, deny_shell=agent.deny_shell, deny_paths=agent.deny_paths,
                   **budget)  # fmt: skip

    @property
    def trees_root(self) -> Path:
        """The folder holding this run's trees: the only place its agents may write."""
        return self.target / self.policy["worktrees_root"] / self.run_id

    # ── commands ──────────────────────────────────────────────────────────────

    def command(self, command: str) -> list[Block]:
        """Every reason this shell command is refused. Reasons never quote the
        command, so a credential typed in it does not reach a log."""
        blocks: dict[tuple[str, str], Block] = {}
        spellings = _spellings(command)
        for spelling in spellings:
            for reason in policy.destructive(self.policy, spelling):
                why = scrub(reason.replace(repr(spelling), "the command", 1))
                blocks.setdefault(("destructive", why), Block("destructive", why))
        layers = (("denied-command", self.deny_shell, "org, team or agent policy"),
                  ("framework", FRAMEWORK_DENY_SHELL, "framework floor"))  # fmt: skip
        for wall, rules, owner in layers:
            for rule in rules:
                if any(_denies(rule, spelling) for spelling in spellings):
                    why = f"the command matches the deny rule {rule!r} ({owner})"
                    blocks.setdefault((wall, why), Block(wall, why))
        return list(blocks.values())

    # ── writes ────────────────────────────────────────────────────────────────

    def writes(self, paths: Sequence[Path | str]) -> list[Block]:
        """Every reason writing these paths is refused."""
        paths = list(paths)
        blocks = [
            Block("worktree", r)
            for r in policy.confinement(self.policy, self.target, self.run_id, paths)
        ]
        blocks += [Block("protected", r) for r in policy.protected(self.policy, self.target, paths)]
        homes = self._homes()
        for raw in paths:
            # Globs are written relative to a repository ("infra/prod/**"); an agent
            # writes inside a component's tree, so match from that tree's top.
            root = next((h for h in homes if _inside(Path(raw), h)), self.target)
            for wall, globs, owner in (("framework", FRAMEWORK_PROTECTED, "framework floor"),
                                       ("denied-path", self.deny_paths, "paths")):  # fmt: skip
                for glob in globs:
                    if policy.protected({"protected_paths": [glob]}, root, [raw]):
                        blocks.append(Block(wall, f"{_shown(Path(raw), root)}: matches the deny "
                                                  f"rule {glob!r} ({owner})"))  # fmt: skip
        return blocks

    def _homes(self) -> list[Path]:
        """The run's component trees, deepest first (Zyroflo's record)."""
        try:
            trees = RunWorktrees(self.target, self.run_id).trees()
        except WorktreeError:
            trees = {}
        homes = [t.path for t in trees.values() if t.mode == "worktree"]
        return sorted(homes, key=lambda p: len(p.parts), reverse=True)

    # ── diff and push ─────────────────────────────────────────────────────────

    def diff(self, diff_text: str) -> list[Block]:
        """Whether one batch is small enough to review (Zyroflo's budget)."""
        return [Block("budget", r) for r in policy.budget(self.policy, diff_text)]

    def push(self, remote: str, branch: str) -> list[Block]:
        """Whether this push goes to the run's own branch on an approved remote."""
        if not remote:
            return [Block("push", "a push must name its remote")]
        return [Block("push", r)
                for r in policy.push_target(self.policy, remote, branch, self.run_id)]  # fmt: skip


# ── inputs ────────────────────────────────────────────────────────────────────


def _rules(name: str, value: Iterable[str]) -> tuple[str, ...]:
    """A list of non-blank rules - never one string read letter by letter."""
    if isinstance(value, str):
        raise GuardError(f"{name} must be a list of rules, not one string ({value!r})")
    rules = tuple(value)
    if bad := [r for r in rules if not isinstance(r, str) or not r.strip()]:
        raise GuardError(f"{name}: every rule must be non-blank text, not {bad[0]!r}")
    return tuple(r.strip() for r in rules)


def _budget(key: str, value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GuardError(f"{key} must be a whole number of at least 1, not {value!r}")
    return value


def _tighter(base: int, requested: int | None) -> int:
    """The smaller budget; Zyroflo reads 0 as "no limit"."""
    if requested is None:
        return base
    return requested if not base else min(base, requested)


# ── how a command is read ─────────────────────────────────────────────────────

#: `then`/`do`/`!` and redirects at a segment's start hide the program from a tokenizer.
_STRUCTURE = re.compile(r"(?:(?<=^)|(?<=[\s;&|(]))(?:then|do|else|elif|!|\d*[<>]+&?[^\s;&|]*)"
                        r"(?=\s|$)")  # fmt: skip
_QUOTED_PATH = re.compile(r"""(["'])([^"']*[\\/][^"']*)\1""")
_WORDS = re.compile(r"[\s;&|()<>`'\"{}]+|\$\(")
_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".sh")


def _spellings(command: str) -> list[str]:
    """The command as typed, and as it reads once structure, quotes and quoted
    paths no longer hide its programs. Every spelling is asked; any hit refuses."""
    structural = _STRUCTURE.sub(";", command)
    out: dict[str, None] = {}
    for base in (command, structural):
        paths = _QUOTED_PATH.sub(lambda m: _basename(m.group(2)), base)
        for text in (base, paths):
            light = re.sub(r"""['"^`]""", "", text)
            out.update({text: None, light: None, light.replace("\\", ""): None})
    return list(out)


def _denies(rule: str, command: str) -> bool:
    if policy.denied_command({"deny_commands": [rule]}, command):
        return True
    # Word by word, too: Zyroflo looks THROUGH wrappers (sudo, env, nohup, …) to
    # the command they run, and an 8.3 short name (KUBECT~1) is another spelling
    # of the program. A deny list may over-refuse, never under-refuse.
    parts = rule.split()
    if not parts:
        return False
    words = [w for w in _WORDS.split(command) if w]
    lowered = {w.lower() for w in words}
    if not any(_same_program(_program(w), _program(parts[0])) for w in words):
        return False
    return all(any(alt.lower() in lowered for alt in p.split("|")) for p in parts[1:])


def _same_program(name: str, wanted: str) -> bool:
    if name == wanted:
        return True
    stem, tilde, _ = name.partition("~")  # KUBECT~1 is kubectl's 8.3 short name
    return bool(tilde) and len(stem) >= 1 and wanted.startswith(stem)


def _program(word: str) -> str:
    name = _basename(word).lower()
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


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
