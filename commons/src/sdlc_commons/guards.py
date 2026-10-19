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

import itertools
import os
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
    "**/.git",
    "**/.git/**",
    "**/.github/workflows/**",
    "**/.claude/**",
    "**/CLAUDE.md",
    "**/AGENTS.md",
    "**/.agentic/**",
    "**/.husky/**",
    "**/.pre-commit-config.yaml",
    "**/.gitmodules",
    "**/.vscode/**",
    "**/.devcontainer/**",
)
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
        allow_shell: Iterable[str] | None = None,
        ask_shell: Iterable[str] = (),
    ) -> None:
        self.target = Path(target)
        self.run_id = run_id
        self.deny_shell = _rules("deny_shell", deny_shell)
        self.deny_paths = _rules("deny_paths", deny_paths)
        # None = no allow-list check (adapter-level use); () = nothing is allowed
        self.allow_shell = None if allow_shell is None else _rules("allow_shell", allow_shell)
        self.ask_shell = _rules("ask_shell", ask_shell)
        try:
            repo = policy.load(self.target)  # the repo's policy, or Zyroflo's defaults
        except policy.PolicyError as error:
            raise GuardError(str(error)) from error
        # A new dict, never an edit of Zyroflo's: `load` returns its defaults' lists.
        self.policy = {**repo}
        for key in _FLOOR_LISTS:
            self.policy[key] = list(dict.fromkeys([*policy.DEFAULTS[key], *repo[key]]))
        for key, requested in (
            ("max_files_per_batch", max_files),
            ("max_lines_per_batch", max_lines),
        ):
            floor = _tighter(policy.DEFAULTS[key], repo[key] or None)
            self.policy[key] = _tighter(floor, _budget(key, requested))

    @classmethod
    def for_agent(cls, target: Path, run_id: str, agent: object, **budget: int) -> "Guard":
        """A guard for a resolved agent: its deny lists (D31), its allowed command
        prefixes (`shell`) and the prefixes a person approves each time (`ask_shell`)."""
        return cls(
            target,
            run_id,
            deny_shell=agent.deny_shell,
            deny_paths=agent.deny_paths,
            allow_shell=agent.shell,
            ask_shell=agent.ask_shell,
            **budget,
        )

    @property
    def trees_root(self) -> Path:
        """The folder holding this run's trees: the only place its agents may write."""
        return self.target / self.policy["worktrees_root"] / self.run_id

    # ── commands ──────────────────────────────────────────────────────────────

    def command(self, command: str) -> list[Block]:
        """Every reason this shell command is refused. Reasons never quote the
        command, so a credential typed in it does not reach a log."""
        if len(command) > MAX_COMMAND:
            return [
                Block(
                    "framework",
                    f"the command is longer than {MAX_COMMAND} characters "
                    f"- refused unread (framework floor)",
                )
            ]
        blocks: dict[tuple[str, str], Block] = {}
        spellings = _spellings(command)
        for spelling in spellings:
            for reason in policy.destructive(self.policy, spelling):
                why = scrub(reason.replace(repr(spelling), "the command", 1))
                blocks.setdefault(("destructive", why), Block("destructive", why))
        try:
            commands = [c for spelling in _rule_spellings(command) for c in _commands(spelling)]
        except _TooComplex:
            return [
                Block(
                    "framework",
                    f"the command nests more than {_MAX_POSITIONS} programs "
                    f"in one segment - refused unread (framework floor)",
                )
            ]
        layers = (
            ("denied-command", self.deny_shell, "org, team or agent policy"),
            ("framework", FRAMEWORK_DENY_SHELL, "framework floor"),
        )
        for wall, rules, owner in layers:
            for rule in rules:
                if _rule_hits(rule, commands):
                    why = f"the command matches the deny rule {rule!r} ({owner})"
                    blocks.setdefault((wall, why), Block(wall, why))
        if self.allow_shell is not None:  # every command must be allowed (DF-16)
            for block in self._allow_refusals(command):
                blocks.setdefault((block.wall, block.why), block)
        if _ANSI_QUOTING.search(command):
            why = "$'...' and $\"...\" quoting can hide a program (framework floor)"
            blocks.setdefault(("framework", why), Block("framework", why))
        if any(_GIT_ENV.match(word) for spelling in spellings for word in _words(spelling)):
            why = "a GIT_* variable can make git run a command (framework floor)"
            blocks.setdefault(("framework", why), Block("framework", why))
        return list(blocks.values())

    def _allow_refusals(self, command: str) -> list[Block]:
        """Every command in `command` that no allow rule covers.

        An allow rule is a word prefix of one command ("uv run pytest" covers
        "uv run pytest -q", never "uv run curl"). Every command counts: each segment
        between `; | & && ||` and newlines, and what an interpreter is told to run
        (`bash -c "..."`, `find -exec ...`). Text the reader cannot follow - command
        substitution, `${...}` expansion, process substitution, comments - is refused
        whole: it can hide a program (review C1)."""
        if _UNREADABLE.search(command):
            why = (
                "command substitution, ${...} expansion and # comments cannot be checked "
                "against the allow list - write the command without them (framework floor)"
            )
            return [Block("not-allowed", why)]
        text = _FD_REDIRECT.sub(" ", _BOTH_PIPE.sub("|", _BOTH_REDIRECT.sub(">", command)))
        rules = (*self.allow_shell, *ALWAYS_ALLOWED)
        refused: dict[str, None] = {}
        for spelling in dict.fromkeys(
            (text, text.replace("\\", ""), _with_paths_as_programs(text))
        ):
            for words in _allow_segments(spelling):
                if not any(_prefix_hits(rule, words) for rule in rules):
                    refused[_program(words[0]) if words else "?"] = None
        return [
            Block(
                "not-allowed", f"{program!r} used this way is not on the allow list (agent policy)"
            )
            for program in refused
        ]

    def asks(self, command: str) -> list[str]:
        """The ask rules this command hits: it runs only after a person approves it.
        Matched like the deny rules (asking too often is the safe side)."""
        try:
            commands = [c for spelling in _rule_spellings(command) for c in _commands(spelling)]
        except _TooComplex:
            return list(self.ask_shell)
        return [rule for rule in self.ask_shell if _ask_hits(rule, commands)]

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
            if _case_insensitive(root):  # macOS: `Infra/x` IS `infra/x` (DF-10)
                rel = _shown(Path(raw), root).lower()
                folded = [p.lower() for p in self.policy["protected_paths"]]
                if policy.matches(rel, folded) and not any(b.wall == "protected" for b in blocks):
                    why = f"{rel}: a protected path - the CLIs write it, agents do not"
                    blocks.append(Block("protected", why))
            for wall, globs, owner in (
                ("framework", FRAMEWORK_PROTECTED, "framework floor"),
                ("denied-path", self.deny_paths, "paths"),
            ):
                for glob in globs:
                    hit = policy.protected({"protected_paths": [glob]}, root, [raw])
                    if not hit and _case_insensitive(root):
                        hit = policy.matches(_shown(Path(raw), root).lower(), [glob.lower()])
                    if hit:
                        why = f"{_shown(Path(raw), root)}: matches the deny rule {glob!r} ({owner})"
                        blocks.append(Block(wall, why))
        return blocks

    # ── compiled for the OS sandbox and other backends (M1-31) ─────────────────

    def write_denials(self, tree: Path) -> "WriteDenials":
        """The write denials `srt` can enforce in `tree`: literal paths only (Linux
        binds concrete paths). `x` and `x/**` become `tree/x`; `**/x` also denies
        `tree/x` but stays listed as unenforced, because a deeper `x` is not covered;
        any other glob is unenforced - the file tools still refuse it."""
        tree = Path(tree)
        paths: dict[Path, None] = {}
        unenforced: dict[str, None] = {}
        for pattern in self._path_rules():
            rest = pattern[3:] if pattern.startswith("**/") else pattern
            if rest.endswith("/**"):
                rest = rest[:-3]
            if rest and not _GLOB_CHARS & set(rest):
                paths[tree / rest] = None
            if rest != pattern.removesuffix("/**") or _GLOB_CHARS & set(rest) or not rest:
                unenforced[pattern] = None
        return WriteDenials(paths=tuple(paths), unenforced=tuple(unenforced))

    def claude_permissions(self) -> dict:
        """The shell policy in Claude Code's settings format (`permissions`): allow,
        ask and deny prefixes as `Bash(prefix:*)`, denied paths as `Edit(glob)`."""
        deny = [*_bash_rules([*self.deny_shell, *FRAMEWORK_DENY_SHELL])]
        deny += [f"Edit({pattern})" for pattern in self._path_rules()]
        return {
            "permissions": {
                "allow": _bash_rules(self.allow_shell or ()),
                "ask": _bash_rules(self.ask_shell),
                "deny": list(dict.fromkeys(deny)),
            }
        }

    def _path_rules(self) -> list[str]:
        """Every write-deny pattern: Zyroflo's protected paths, the framework's, policy's."""
        sources = (self.policy["protected_paths"], FRAMEWORK_PROTECTED, self.deny_paths)
        return list(dict.fromkeys(pattern for source in sources for pattern in source))

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
        return [
            Block("push", r) for r in policy.push_target(self.policy, remote, branch, self.run_id)
        ]


@dataclass(frozen=True)
class WriteDenials:
    """What `srt` can deny writing in a tree, and the patterns it cannot express."""

    paths: tuple[Path, ...]
    unenforced: tuple[str, ...]


_GLOB_CHARS = set("*?[")


def _bash_rules(rules: Iterable[str]) -> list[str]:
    """`Bash(prefix:*)` for each prefix, with `a|b` alternatives spelled out."""
    out: dict[str, None] = {}
    for rule in rules:
        words = [word.split("|") for word in rule.split()]
        for combination in itertools.product(*words):
            out[f"Bash({' '.join(combination)}:*)"] = None
    return list(out)


def _case_insensitive(path: Path) -> bool:
    """Whether the file system holding `path` ignores case (macOS, Windows)."""
    text = str(path)
    swapped = text.swapcase()
    if swapped == text:
        return False
    try:
        return Path(swapped).exists() and os.path.samefile(text, swapped)
    except OSError:
        return False


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
# be at a command position - a segment's start (after `VAR=value` and control
# keywords), one of the first words after a program that runs its arguments
# (`sudo`, `ssh host`, `docker run image`), or the word an interpreter is told to
# run (`bash -lc`, `cmd /c`, `powershell -Command`, `find -exec`) - and its first
# plain word must be one of that program's first plain words. So `git push`
# refuses `git -C repo push` and never `git commit -m push` or `git log --grep
# push`; `eval` refuses `eval …` and never `make eval`.

#: Control keywords, and redirects (with their target), as separators: they hide
#: the program that follows from a tokenizer. A here-string keeps its text.
_STRUCTURE = re.compile(
    r"(?:(?<=^)|(?<=[\s;&|(]))"
    r"(?:if|while|until|select|elif|then|do|else|!|<<<|\d*[<>]+&?\s*[^\s;&|<>]*)"
    r"(?=\s|$)"
)
_SEPARATORS = frozenset(";|&()<>{}`\n\r")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_GIT_ENV = re.compile(r"GIT_[A-Z0-9_]*=")
_ANSI_QUOTING = re.compile(r"""\$['"]""")
#: Words skipped at a segment's start: they are not the program.
_TRANSPARENT = frozenset(
    {"if", "while", "until", "select", "elif", "then", "do", "else", "!", "{", "time", "coproc"}
)
#: Programs that run a command given among their first few arguments.
_WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "command",
        "builtin",
        "nohup",
        "exec",
        "timeout",
        "stdbuf",
        "nice",
        "ionice",
        "setsid",
        "xargs",
        "parallel",
        "watch",
        "chroot",
        "runuser",
        "su",
        "pkexec",
        "runas",
        "start",
        "start-process",
        "call",
        "ssh",
        "wsl",
        "npx",
        "uv",
        "uvx",
        "pipx",
        "bunx",
        "strace",
        "ltrace",
        "flock",
        "docker",
        "podman",
    }
)
_WRAPPER_REACH = 3  # how many plain words after a wrapper may be the command
_MAX_POSITIONS = 32  # more programs nested in one segment is refused unread
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"})
_SHELL_RUN = re.compile(r"-[A-Za-z]*c[A-Za-z]*")  # -c, -lc, -ec, -xc …
#: Options whose value is a plain word that is not a subcommand.
_VALUE_OPTIONS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--exec-path",
        "-n",
        "--context",
        "--kubeconfig",
        "-m",
        "--message",
        "-F",
        "--file",
        "--grep",
        "-S",
        "-G",
        "--author",
        "-e",
        "--regexp",
    }
)
_SUBCOMMAND_REACH = 3  # how many plain words after the program may be the subcommand
_POWERSHELL = frozenset({"powershell", "pwsh"})
_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".sh")


class _TooComplex(Exception):
    """A segment nests more programs than the guard will follow."""


@dataclass(frozen=True)
class _Word:
    text: str  # quotes removed; for a separator, its character
    quoted: bool = False
    sep: bool = False


def _tokens(text: str) -> list[_Word]:
    """Words and separators in ONE pass: quoted text belongs to its word
    (`de""lete` is `delete`, `C:\\"Program Files"\\x` is one word); `;|&(){}<>`,
    backticks, newlines and `$(` separate commands."""
    out: list[_Word] = []
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
        elif c in _SEPARATORS or c.isspace() or (c == "$" and text[i + 1 : i + 2] == "("):
            if buf or quoted:
                out.append(_Word("".join(buf), quoted))
            buf, quoted = [], False
            if not c.isspace() or c in _SEPARATORS:
                out.append(_Word("(" if c == "$" else c, sep=True))
                i += c == "$"
        else:
            buf.append(c)
        i += 1
    if buf or quoted:
        out.append(_Word("".join(buf), quoted))
    return out


def _words(text: str) -> list[str]:
    return [t.text for t in _tokens(text) if not t.sep]


def _spellings(command: str) -> list[str]:
    """The command as typed, and as it reads once structure (keywords,
    redirects), cmd's doubled quotes, quotes and quoted program paths no longer
    hide its programs."""
    out: dict[str, None] = {}
    for base in (command, command.replace('""', '"')):
        for shaped in (base, _STRUCTURE.sub(";", base)):
            for text in (shaped, _with_paths_as_programs(shaped)):
                light = re.sub(r"""['"^`]""", "", text)
                out.update({text: None, light: None, light.replace("\\", ""): None})
    return list(out)


def _rule_spellings(command: str) -> list[str]:
    """The spellings the deny rules are asked of: quotes KEPT, so a quoted
    argument (a commit message, a grep pattern) stays one word; the tokenizer
    already joins quotes inside a word (`c''url` is `curl`)."""
    out: dict[str, None] = {}
    for base in (command, command.replace('""', '"')):
        for shaped in (base, _STRUCTURE.sub(";", base)):
            for text in (shaped, _with_paths_as_programs(shaped)):
                out.update({text: None, text.replace("\\", ""): None})  # `su\do` is `sudo`
    return list(out)


def _with_paths_as_programs(text: str) -> str:
    """Each quoted word that is a path, as its file name: `"C:\\Program
    Files\\Git\\usr\\bin\\rm.exe" -rf x` reads `rm.exe -rf x`."""
    parts = []
    for token in _tokens(text):
        if token.sep:
            parts.append(token.text if token.text not in "\n\r" else ";")
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
    after = ""  # the separator before this segment
    for token in [*_tokens(text), _Word(";", sep=True)]:
        if not token.sep:
            segment.append(token)
            continue
        start = 1 if after in "<>" and after else 0  # a redirect's target is not a command
        while start < len(segment) and (
            _ENV_ASSIGNMENT.match(segment[start].text) or segment[start].text in _TRANSPARENT
        ):
            start += 1
        for j in _positions(segment, start):
            word = segment[j]
            found.append((_program(word.text), [w.text for w in segment[j + 1 :]]))
            if word.quoted and depth < 3 and any(ch.isspace() for ch in word.text):
                found += _commands(word.text, depth + 1)  # `bash -c 'kubectl delete …'`
        segment, after = [], token.text
    return found


def _positions(segment: list[_Word], start: int) -> list[int]:
    """The command positions of one segment, from its program onward."""
    if start >= len(segment):
        return []
    positions, queue = {start}, [start]
    while queue:
        j = queue.pop()
        program = _program(segment[j].text)
        later = []
        if program in _WRAPPERS:  # `sudo -u root X`, `ssh host X`, `docker run --rm img X`
            plain = [
                k
                for k in range(j + 1, len(segment))
                if not segment[k].text.startswith("-")
                and not _ENV_ASSIGNMENT.match(segment[k].text)
            ]
            later = plain[:_WRAPPER_REACH]
        if (k := _carried(segment, j, program)) is not None:
            later.append(k)
        for k in later:
            if k not in positions:
                positions.add(k)
                queue.append(k)
        if len(positions) > _MAX_POSITIONS:
            raise _TooComplex
    return sorted(positions)


def _carried(segment: list[_Word], j: int, program: str) -> int | None:
    """The position of the command an interpreter at `j` is told to run."""
    for k in range(j + 1, len(segment) - 1):
        word = segment[k].text
        lowered = word.lower()
        shell = program in _SHELLS and _SHELL_RUN.fullmatch(word)
        cmd = program == "cmd" and lowered in ("/c", "/k", "/r")
        powershell = program in _POWERSHELL and len(lowered) >= 2 and "-command".startswith(lowered)
        find = program == "find" and lowered in ("-exec", "-execdir", "-ok", "-okdir")
        if shell or cmd or powershell or find:
            return k + 1
    return None


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
        if plain and not set(plain[0]) & {s.lower() for s in _subcommands(args)}:
            continue
        if not all(any(alt in lowered for alt in alts) for alts in plain[1:]):
            continue
        prefix = wanted in _POWERSHELL  # PowerShell takes any prefix of a parameter name
        # git's global options (`git -c …`, `--config-env`) stand before the subcommand;
        # after it, `-c` is another command's own flag (`git grep -c`).
        where = _before_subcommand(args) if wanted == "git" and not plain else lowered
        if all(any(_option_hit(alt, where, prefix) for alt in alts) for alts in options):
            return True
    return False


#: Shell builtins that run nothing and write nothing: allowed without listing them.
ALWAYS_ALLOWED = ("cd", "pwd", "true", "false", "exit", "set", "export", "unset", "test", "[", ":")
#: Text the allow-list reader cannot follow: it is refused whole (review C1).
_UNREADABLE = re.compile(r"`|\$\(|\$\{|<\(|>\(|(?:^|\s)#")
_BOTH_PIPE = re.compile(r"\|&")  # `a |& b` pipes both streams: still a pipe
_BOTH_REDIRECT = re.compile(r"&>>?")  # `&> file` is a redirect
_FD_REDIRECT = re.compile(r"\d*[<>]&\d*-?")  # `2>&1`, `>&2`: no file, no program
_MAX_DEPTH = 3


def _allow_segments(text: str, depth: int = 0) -> list[list[str]]:
    """The words of every command in `text`, from its program onward: one list per
    segment, plus what an interpreter in it is told to run."""
    if depth > _MAX_DEPTH:
        return [["(nested too deep)"]]
    found: list[list[str]] = []
    segment: list[_Word] = []
    after = ""
    for token in [*_tokens(text), _Word(";", sep=True)]:
        if not token.sep:
            segment.append(token)
            continue
        start = 1 if after in "<>" and after else 0  # a redirect's target is not a command
        while start < len(segment) and (
            _ENV_ASSIGNMENT.match(segment[start].text) or segment[start].text in _TRANSPARENT
        ):
            start += 1
        if start < len(segment):
            found.append([w.text for w in segment[start:]])
            carried = _carried(segment, start, _program(segment[start].text))
            if carried is not None:
                word = segment[carried]
                if word.quoted and any(ch.isspace() for ch in word.text):
                    found += _allow_segments(word.text, depth + 1)
                else:
                    found.append([w.text for w in segment[carried:]])
        segment, after = [], token.text
    return found


def _prefix_hits(rule: str, words: list[str]) -> bool:
    """Whether an allow rule is a word prefix of one command: the same program (exact
    name, no 8.3 leniency), then the rule's words in order; `a|b` are alternatives."""
    wanted = rule.split()
    if not words or len(words) < len(wanted):
        return False
    if _program(words[0]) not in {_program(o) for o in wanted[0].split("|")}:
        return False
    return all(word in alts.split("|") for alts, word in zip(wanted[1:], words[1:], strict=False))


def _ask_hits(rule: str, commands: list[tuple[str, list[str]]]) -> bool:
    """Whether an ask rule matches a command: the same program and each of the rule's
    words ANYWHERE among its arguments - options before a subcommand cannot hide it
    (review I1); asking once too often is the safe side."""
    head, *parts = rule.split()
    wanted = _program(head)
    plain = [p.lower().split("|") for p in parts if not p.startswith("-")]
    options = [p.lower().split("|") for p in parts if p.startswith("-")]
    for name, args in commands:
        if not _same_program(name, wanted):
            continue
        lowered = [a.lower() for a in args]
        if all(any(alt in lowered for alt in alts) for alts in plain) and all(
            any(_option_hit(alt, lowered, False) for alt in alts) for alts in options
        ):
            return True
    return False


def _before_subcommand(args: list[str]) -> list[str]:
    """The options before a program's first plain word, case kept: for git, `-C`
    (change directory) is not `-c` (set config)."""
    head, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        head.append(args[i])
        i += 2 if args[i] in _VALUE_OPTIONS else 1
    return head


def _subcommands(args: list[str]) -> list[str]:
    """The first plain words after a program - one of them is its subcommand
    (`kubectl --as admin delete`: admin, delete)."""
    found, i = [], 0
    while i < len(args) and len(found) < _SUBCOMMAND_REACH:
        if args[i] in _VALUE_OPTIONS:
            i += 2
            continue
        if not args[i].startswith("-"):
            found.append(args[i])
        i += 1
    return found


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
