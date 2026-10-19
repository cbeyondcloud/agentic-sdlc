"""Guard adapter over Zyroflo's walls (M1-08; D31 deny lists; D34: reuse, never rewrite).

May an agent run this command, write these paths, hand over this diff, push
there? Zyroflo's guard functions (`tools.sdlc.policy`) judge Zyroflo's walls;
this module adds the framework's rules on top:

  * command - Zyroflo's destructive defaults (`destructive`), plus the agent's
              resolved deny list (org + team + agent, D31) and the framework's
              floor (`FRAMEWORK_DENY_SHELL`), matched by where a word stands (a
              rule's program at a command position, its first word the
              subcommand - see "how a command is read"). Each is asked of the
              command as typed AND of the spellings that hide a program from a
              tokenizer (quotes inside a word, a quoted Windows path, a leading
              redirect, `then`/`do`/`!`). Any hit refuses. A command longer than
              MAX_COMMAND is refused unread;
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
    # git options, subcommands and tools that run an arbitrary command
    "git -c",  # `git -c alias.x='!cmd'`, `core.sshCommand=…`
    "git --config-env",
    "git config",
    "git submodule foreach",
    "git rebase -x|--exec",
    "git bisect run",
    "git difftool",
    "git mergetool",
    # publishing is the framework's job (M1-58), never an agent's
    "git push",
    "git remote",
    # text run as a command cannot be checked
    "eval",
    "iex",
    "invoke-expression",
    "busybox",
    "powershell -e|-ec|-enc|-encodedcommand",  # base64 hides the command
    "pwsh -e|-ec|-enc|-encodedcommand",
    # privilege
    "doas",
    "su",
    "pkexec",
    "runas",
)
#: Files inside a tree that steer agents, hooks, editors or CI: never written by an agent.
FRAMEWORK_PROTECTED = (
    "**/.git", "**/.git/**", "**/.github/workflows/**", "**/.claude/**", "**/CLAUDE.md",
    "**/AGENTS.md", "**/.agentic/**", "**/.husky/**", "**/.pre-commit-config.yaml",
    "**/.gitmodules", "**/.vscode/**", "**/.devcontainer/**",
)  # fmt: skip
#: A longer command is refused unread: judging it would cost more than it is worth.
MAX_COMMAND = 16_384
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
        if len(command) > MAX_COMMAND:
            return [Block("framework", f"the command is longer than {MAX_COMMAND} characters "
                                       f"- refused unread (framework floor)")]  # fmt: skip
        blocks: dict[tuple[str, str], Block] = {}
        spellings = _spellings(command)
        for spelling in spellings:
            for reason in policy.destructive(self.policy, spelling):
                why = scrub(reason.replace(repr(spelling), "the command", 1))
                blocks.setdefault(("destructive", why), Block("destructive", why))
        commands = [c for spelling in spellings for c in _commands(spelling)]
        layers = (("denied-command", self.deny_shell, "org, team or agent policy"),
                  ("framework", FRAMEWORK_DENY_SHELL, "framework floor"))  # fmt: skip
        for wall, rules, owner in layers:
            for rule in rules:
                if _rule_hits(rule, commands):
                    why = f"the command matches the deny rule {rule!r} ({owner})"
                    blocks.setdefault((wall, why), Block(wall, why))
        if any(_GIT_ENV.match(word) for spelling in spellings for word in _words(spelling)):
            why = "a GIT_* variable can make git run a command (framework floor)"
            blocks.setdefault(("framework", why), Block("framework", why))
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
#
# Zyroflo's `destructive` judges its own defaults. The deny lists and the
# framework floor are judged here, by WHERE a word stands: a rule's program must
# be at a command position (a segment's start, after a wrapper like `sudo`, after
# `-c` or `-exec`), and its first plain word must be that program's subcommand.
# So `git push` refuses `git -C repo push` and never `git commit -m "add push"`
# or `git log --grep push`; `eval` refuses `eval …` and never `make eval`.

#: `then`/`do`/`!` and redirects at a segment's start hide the program from a tokenizer.
_STRUCTURE = re.compile(r"(?:(?<=^)|(?<=[\s;&|(]))(?:then|do|else|elif|!|\d*[<>]+&?[^\s;&|]*)"
                        r"(?=\s|$)")  # fmt: skip
_SEPARATORS = frozenset(";|&()<>{}`\n")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_GIT_ENV = re.compile(r"GIT_[A-Z0-9_]*=")
#: Programs that run the command that follows them.
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "command", "builtin", "nohup", "time", "exec", "timeout", "stdbuf",
    "nice", "ionice", "setsid", "xargs", "parallel", "watch", "chroot", "runuser", "su",
    "pkexec", "runas", "start", "start-process", "call",
})  # fmt: skip
#: Options whose value is a command.
_CARRIERS = frozenset({"-c", "/c", "/k", "-command", "--command", "-exec", "-execdir", "-ok"})
#: Options that take the next word as their value, so it is not the subcommand.
_VALUE_OPTIONS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path",
    "-n", "--context", "--kubeconfig",
})  # fmt: skip
_POWERSHELL = frozenset({"powershell", "pwsh"})
_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".sh")


@dataclass(frozen=True)
class _Word:
    text: str  # quotes removed
    quoted: bool


def _tokens(text: str) -> list[_Word | None]:
    """Words and separators (None), in ONE pass: quoted text belongs to its word
    (`de""lete` is `delete`, `C:\\"Program Files"\\x` is one word); `;|&(){}<>`,
    backticks, newlines and `$(` separate commands."""
    out: list[_Word | None] = []
    buf: list[str] = []
    quoted, quote, i = False, "", 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = ""
            else:
                buf.append(c)
        elif c in "'\"":
            quote, quoted = c, True
        elif c.isspace() or c in _SEPARATORS or (c == "$" and text[i + 1 : i + 2] == "("):
            if buf or quoted:
                out.append(_Word("".join(buf), quoted))
            buf, quoted = [], False
            if not c.isspace():
                out.append(None)
                i += c == "$"
        else:
            buf.append(c)
        i += 1
    if buf or quoted:
        out.append(_Word("".join(buf), quoted))
    return out


def _words(text: str) -> list[str]:
    return [t.text for t in _tokens(text) if t is not None]


def _spellings(command: str) -> list[str]:
    """The command as typed, and as it reads once structure (`then`, `!`,
    redirects), quotes and quoted program paths no longer hide its programs."""
    structural = _STRUCTURE.sub(";", command)
    out: dict[str, None] = {}
    for base in (command, structural):
        for text in (base, _with_paths_as_programs(base)):
            light = re.sub(r"""['"^`]""", "", text)
            out.update({text: None, light: None, light.replace("\\", ""): None})
    return list(out)


def _with_paths_as_programs(text: str) -> str:
    """Each quoted word that is a path, as its file name: `"C:\\Program
    Files\\Git\\usr\\bin\\rm.exe" -rf x` reads `rm.exe -rf x`."""
    parts = []
    for token in _tokens(text):
        if token is None:
            parts.append(";")
        elif token.quoted and ("/" in token.text or "\\" in token.text):
            parts.append(_basename(token.text))
        elif token.quoted:
            parts.append('"' + token.text.replace('"', "") + '"')
        else:
            parts.append(token.text)
    return " ".join(parts)


def _commands(text: str, depth: int = 0) -> list[tuple[str, list[str]]]:
    """(program, the words after it in its segment) at every command position."""
    found: list[tuple[str, list[str]]] = []
    segment: list[_Word] = []
    for token in [*_tokens(text), None]:
        if token is not None:
            segment.append(token)
            continue
        start = 0
        while start < len(segment) and _ENV_ASSIGNMENT.match(segment[start].text):
            start += 1  # `FOO=1 git push`: the program comes after the assignments
        positions = {start} if start < len(segment) else set()
        if positions and _program(segment[start].text) in _WRAPPERS:
            positions |= set(range(start + 1, len(segment)))  # `sudo -u root rm …`
        positions |= {j + 1 for j, w in enumerate(segment[:-1]) if w.text.lower() in _CARRIERS}
        for j in sorted(positions):
            word = segment[j]
            found.append((_program(word.text), [w.text for w in segment[j + 1 :]]))
            if word.quoted and depth < 3 and any(ch.isspace() for ch in word.text):
                found += _commands(word.text, depth + 1)  # `bash -c 'kubectl delete …'`
        segment = []
    return found


def _rule_hits(rule: str, commands: list[tuple[str, list[str]]]) -> bool:
    """Whether a deny rule (`program [subcommand] [words] [-options]`, `a|b` for
    alternatives) matches one of the commands."""
    head, *parts = rule.split()
    wanted = _program(head)
    plain = [p.lower().split("|") for p in parts if not p.startswith("-")]
    options = [p.lower().split("|") for p in parts if p.startswith("-")]
    for name, args in commands:
        if not _same_program(name, wanted):
            continue
        lowered = [a.lower() for a in args]
        if plain and (_subcommand(args) or "").lower() not in plain[0]:
            continue
        if not all(any(alt in lowered for alt in alts) for alts in plain[1:]):
            continue
        prefix = wanted in _POWERSHELL  # PowerShell takes any prefix of a parameter name
        if all(any(_option_hit(alt, lowered, prefix) for alt in alts) for alts in options):
            return True
    return False


def _subcommand(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        if args[i] in _VALUE_OPTIONS:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return None


def _option_hit(alt: str, words: list[str], prefix: bool) -> bool:
    for word in words:
        if word == alt or word.startswith(alt + "="):
            return True
        if prefix and len(word) >= 2 and word.startswith("-") and alt.startswith(word):
            return True
    return False


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
