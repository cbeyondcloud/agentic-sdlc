"""The commit-command override (M2-61, spec §9.2: "repos where raw git is unsafe can set a
commit-command override (Zyroflo feature)").

Zyroflo gives a component whose `vcs.deny_raw_git` is set no tree: it works in its own
checkout, and the harness must never run `git commit`, `git push` or `git worktree` on it.
Code steps on it checkpoint, roll back and publish through the repository's own commands:

  * commit   - Zyroflo's `vcs.commit_command` (`.sdlc/profile.yml`), with `{message}` or
               `{message_file}` (the message in a file in the run folder);
  * rollback - `rollback_commands[<component path>]` (`.agentic/config.yaml`; "." is the
               repository itself), with `{commit}`: put the checkout back at that commit of
               its history and discard uncommitted work, untracked files included;
  * push     - Zyroflo's `vcs.push_command`, with `{branch}` (publishing; optional).

These commands run outside the agent's sandbox, as the framework's own checkpoint commits
do, so a repository alone never decides that they run. Every command:

  * starts with a word prefix org policy allows (`commit_override.allow`), checked again
    before each run, so revoking it stops a run at its next command;
  * passes the guard (`Guard.command`: Zyroflo's destructive floor, the repository's and
    the org's deny lists, the framework floor), with its placeholders unfilled;
  * is never raw git, and never names a file of the checkout that changed since the run's
    base or is uncommitted (an agent could have rewritten the commit script);
  * runs as an argv list (never a shell), placeholders filled as whole words only, in the
    component's checkout, without the framework's `AGENTIC_*` credentials, stdin closed,
    under a timeout - on Windows a batch file gets no argument cmd.exe would reinterpret;
  * is recorded in the run folder (`vcs-commands.jsonl`: time, component, action, the
    command as configured, exit code, duration - never the message).

The override accepted at a run's first `resolve` is kept in the run folder
(`worktrees.OVERRIDE_FILE`) and every later step uses that copy: an agent working in the
checkout cannot change the commands by editing the profile.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from zyroflo.sdlc import policy, state

from sdlc_commons.guards import Guard, GuardError
from sdlc_commons.worktrees import (
    InPlace,
    Tree,
    WorktreeError,
    keep_override,
    kept_override,
    worktree_git,
)

#: Each action's placeholders; the first group's: at least one of them is required.
PLACEHOLDERS = {
    "commit": ("{message}", "{message_file}"),
    "rollback": ("{commit}",),
    "push": ("{branch}",),
}
REQUIRED = {"commit": True, "rollback": True, "push": False}
LOG_FILE = "vcs-commands.jsonl"
MESSAGE_FILE = "vcs-message.txt"
_COMMIT = re.compile(r"[0-9a-f]{7,64}")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")
#: What cmd.exe leaves alone in a batch file's arguments (Windows: Python cannot quote them
#: for it). A space is safe inside the quotes Python adds; `=`, `,` and `;` split arguments.
_WINDOWS = os.name == "nt"
_BATCH_SAFE = re.compile(r"[A-Za-z0-9 ._/:\\@+-]*")
#: The framework's plain settings; every other `AGENTIC_*` variable may be a credential.
_PLAIN_SETTINGS = frozenset(
    {"AGENTIC_HOME", "AGENTIC_TEAM", "AGENTIC_USER", "AGENTIC_DEBUG", "AGENTIC_TRACES"}
)


class OverrideError(WorktreeError):
    """The override is not set, not allowed, refused by the guard, or its command failed."""


def split_command(text: str) -> tuple[str, ...]:
    """A configured command as words: POSIX quoting ('…' and "…"), and a backslash is a
    character, never an escape, so a Windows path is kept as written."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    try:
        words = tuple(lexer)
    except ValueError as error:
        raise OverrideError(f"{text!r} cannot be read as a command: {error}") from None
    if not words:
        raise OverrideError("the command is empty")
    return words


class CommitOverride:
    """The commit-command override of one run's in-place components."""

    def __init__(
        self,
        target: Path,
        run_id: str,
        *,
        allow: Iterable[str] = (),
        rollback_commands: Mapping[str, str] | None = None,
        deny_shell: Iterable[str] = (),
        timeout_s: int = 600,
    ) -> None:
        self.target, self.run_id = Path(target), run_id
        self.allow = tuple(split_command(rule) for rule in allow)
        self.rollback_commands = dict(rollback_commands or {})
        self.deny_shell = tuple(deny_shell)
        self.timeout_s = timeout_s

    # ── accepting ─────────────────────────────────────────────────────────────

    def resolve(self, trees: Mapping[str, Tree]) -> dict[str, InPlace]:
        """The accepted override of each in-place component in `trees`. The first call of a
        run checks everything and keeps the result; later calls return what was kept."""
        kept = kept_override(self.target, self.run_id)
        if kept is not None and set(trees) <= set(kept):
            return {name: kept[name] for name in trees}
        problems, accepted = [], {}
        for name, tree in sorted(trees.items()):
            try:
                accepted[name] = self._accept(name, tree)
            except OverrideError as error:
                problems.append(str(error))
        if problems:
            raise OverrideError("; ".join(problems))
        keep_override(self.target, self.run_id, accepted)
        return accepted

    def _accept(self, name: str, tree: Tree) -> InPlace:
        try:
            component = policy.component(self.target, name)
        except (policy.PolicyError, ValueError, OSError) as error:
            raise OverrideError(f"{name}: its profile entry cannot be read: {error}") from None
        path = str(component.get("path") or ".").replace("\\", "/").strip("/") or "."
        vcs = component.get("vcs") or {}
        texts = {
            "commit": str(vcs.get("commit_command") or "").strip(),
            "rollback": str(self.rollback_commands.get(path) or "").strip(),
            "push": str(vcs.get("push_command") or "").strip(),
        }
        settings = {
            "commit": "vcs.commit_command in .sdlc/profile.yml",
            "rollback": f"rollback_commands['{path}'] in .agentic/config.yaml",
        }
        missing = [settings[a] for a in ("commit", "rollback") if not texts[a]]
        if missing or not self.allow:
            needed = [*missing] + ([] if self.allow else ["commit_override.allow in org policy"])
            raise OverrideError(
                f"component {name} works in place (vcs.deny_raw_git), so its code steps are "
                f"checkpointed and rolled back by its own commands (the commit-command "
                f"override) - set {', '.join(needed)}"
            )
        argv = {a: self._template(name, a, texts[a]) for a in PLACEHOLDERS if texts[a]}
        cmds = InPlace(
            component=name,
            checkout=tree.path,
            base=tree.base,
            commit=argv["commit"],
            rollback=argv["rollback"],
            push=argv.get("push", ()),
        )
        for action in PLACEHOLDERS:
            if getattr(cmds, action):
                self._check(cmds, action)
        if _git(tree.path, "status", "--porcelain"):
            raise OverrideError(
                f"component {name} works in place and its checkout {tree.path} has uncommitted "
                f"changes: a step that starts again discards uncommitted work, which here would "
                f"be yours - commit or set it aside first"
            )
        return cmds

    def _template(self, name: str, action: str, text: str) -> tuple[str, ...]:
        words = split_command(text)
        own = PLACEHOLDERS[action]
        for word in words:
            if ("{" in word or "}" in word) and word not in own:
                raise OverrideError(
                    f"component {name}: the {action} command's {word!r} is not one of its "
                    f"placeholders ({', '.join(own)}), each a whole word"
                )
        if REQUIRED[action] and not any(word in own for word in words):
            raise OverrideError(
                f"component {name}: the {action} command must pass the "
                f"{' or '.join(own)} placeholder (the {_what(action)})"
            )
        if words[0] in own:
            raise OverrideError(f"component {name}: the {action} command names no program")
        return words

    def _check(self, cmds: InPlace, action: str) -> None:
        """Everything that must hold before an override command runs (again at each run)."""
        name, words = cmds.component, getattr(cmds, action)
        if _program(words[0]) == "git":
            raise OverrideError(
                f"component {name}: its {action} command is raw git, which vcs.deny_raw_git "
                f"forbids - name the repository's own tool"
            )
        if not any(_starts_with(words, rule) for rule in self.allow):
            raise OverrideError(
                f"component {name}: its {action} command is not allowed by org policy "
                f"(commit_override.allow names the commands an override may run)"
            )
        shown = " ".join(shlex.quote("VALUE" if w in PLACEHOLDERS[action] else w) for w in words)
        try:
            blocks = Guard(self.target, self.run_id, deny_shell=self.deny_shell).command(shown)
        except GuardError as error:
            raise OverrideError(f"component {name}: the guard cannot be read: {error}") from None
        if blocks:
            raise OverrideError(
                f"component {name}: its {action} command is refused by the guard: "
                + "; ".join(block.why for block in blocks)
            )
        changed = [rel for rel in self._checkout_files(cmds, words) if self._changed(cmds, rel)]
        if changed:
            raise OverrideError(
                f"component {name}: its {action} command uses {', '.join(changed)}, which "
                f"changed in this run or is not committed - it is not run on code an agent "
                f"may have written"
            )

    def _checkout_files(self, cmds: InPlace, words: Sequence[str]) -> list[str]:
        """The command's words that name a file inside the checkout, as checkout paths."""
        found = []
        root = cmds.checkout.resolve()
        for word in words:
            if word.startswith("-") or "{" in word:
                continue
            candidate = Path(word) if Path(word).is_absolute() else cmds.checkout / word
            try:
                rel = candidate.resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            if rel.parts and candidate.is_file():
                found.append(rel.as_posix())
        return found

    def _changed(self, cmds: InPlace, rel: str) -> bool:
        if _git(cmds.checkout, "status", "--porcelain", "--", rel):
            return True
        return bool(cmds.base) and bool(
            _git(cmds.checkout, "diff", "--name-only", cmds.base, "HEAD", "--", rel)
        )

    # ── running ───────────────────────────────────────────────────────────────

    def commit(self, cmds: InPlace, message: str) -> None:
        """Commit the step's work in the checkout through the commit command."""
        folder = state.runs_dir(self.target) / self.run_id
        folder.mkdir(parents=True, exist_ok=True)
        message_file = folder / MESSAGE_FILE
        message_file.write_text(message, encoding="utf-8", newline="\n")
        self._run(cmds, "commit", {"{message}": message, "{message_file}": str(message_file)})

    def rollback(self, cmds: InPlace, commit: str) -> None:
        """Put the checkout back at `commit` through the rollback command."""
        if not _COMMIT.fullmatch(commit):
            raise OverrideError(f"rollback takes a commit id (hex), not {commit!r}")
        self._run(cmds, "rollback", {"{commit}": commit})

    def push(self, cmds: InPlace, branch: str) -> None:
        """Publish the checkout's work to `branch` through the push command."""
        if not _BRANCH.fullmatch(branch) or branch.startswith("-") or ".." in branch:
            raise OverrideError(f"push takes a branch name, not {branch!r}")
        if not cmds.push:
            raise OverrideError(
                f"component {cmds.component} works in place and sets no vcs.push_command in "
                f".sdlc/profile.yml, so it cannot be published"
            )
        self._run(cmds, "push", {"{branch}": branch})

    def _run(self, cmds: InPlace, action: str, values: Mapping[str, str]) -> None:
        self._check(cmds, action)
        template = getattr(cmds, action)
        argv = [values.get(word, word) for word in template]
        argv[0] = self._program_path(cmds, argv[0])
        if _WINDOWS and Path(argv[0]).suffix.lower() in (".bat", ".cmd"):
            if unsafe := [w for w in argv[1:] if not _BATCH_SAFE.fullmatch(w)]:
                raise OverrideError(
                    f"component {cmds.component}: its {action} command is a batch file, and "
                    f"cmd.exe would reinterpret {len(unsafe)} of its arguments - run the tool "
                    f"another way, and pass the message with {{message_file}}"
                )
        started = time.monotonic()
        code: int | None = None
        try:
            done = subprocess.run(
                argv,
                cwd=cmds.checkout,
                env=_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
            code = done.returncode
        except subprocess.TimeoutExpired:
            raise OverrideError(
                f"component {cmds.component}: its {action} command did not finish within "
                f"{self.timeout_s}s - check the checkout {cmds.checkout} by hand"
            ) from None
        except OSError as error:
            raise OverrideError(
                f"component {cmds.component}: its {action} command could not be started "
                f"({type(error).__name__})"
            ) from None
        finally:
            self._record(cmds, action, code, started)
        if code != 0:
            printed = "\n".join(p for p in (done.stderr.strip(), done.stdout.strip()) if p)
            tail = " / ".join(printed.splitlines()[-3:])
            raise OverrideError(
                f"component {cmds.component}: its {action} command failed (exit {code})"
                + (f": {_scrub(tail)}" if tail else "")
            )

    def _program_path(self, cmds: InPlace, program: str) -> str:
        """The program as a path: one given by a path is relative to the checkout; one given
        by name is looked up on PATH (with Windows' PATHEXT)."""
        if "/" in program or "\\" in program:
            path = Path(program) if Path(program).is_absolute() else cmds.checkout / program
            if not path.is_file():
                raise OverrideError(f"component {cmds.component}: {program} is not a file")
            return str(path)
        found = shutil.which(program)
        if found is None:
            raise OverrideError(f"component {cmds.component}: {program} is not on PATH")
        # Windows looks in the current folder before PATH: a program by name found in the
        # checkout or the repository may be an agent's file, run outside the sandbox
        if any(_within(Path(found), folder) for folder in (cmds.checkout, self.target)):
            raise OverrideError(
                f"component {cmds.component}: {program} was found inside the checkout, not on "
                f"PATH - name a tool installed on the machine, or give the checkout's own "
                f"script by its path"
            )
        return found

    def _record(self, cmds: InPlace, action: str, code: int | None, started: float) -> None:
        row = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "component": cmds.component,
            "action": action,
            "command": list(getattr(cmds, action)),
            "exit_code": code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        path = state.runs_dir(self.target) / self.run_id / LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as log:
            log.write(json.dumps(row, sort_keys=True) + "\n")


def without_push(target: Path) -> list[str]:
    """The profile's in-place components that name no push command: a run on them cannot
    be published (asked before a run spends anything). [] when the profile cannot be read -
    the run itself then says why."""
    try:
        components = policy.components(Path(target))
    except (policy.PolicyError, ValueError, OSError):
        return []
    return [
        str(c["name"])
        for c in components
        if (c.get("vcs") or {}).get("deny_raw_git")
        and not str((c.get("vcs") or {}).get("push_command") or "").strip()
    ]


def _what(action: str) -> str:
    return {"commit": "commit message", "rollback": "commit to go back to"}.get(action, "value")


def _starts_with(words: Sequence[str], rule: Sequence[str]) -> bool:
    if len(rule) > len(words):
        return False
    return all(_same(w, r) for w, r in zip(words, rule, strict=False))


def _same(word: str, rule: str) -> bool:
    return word.replace("\\", "/") == rule.replace("\\", "/")


def _within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        return False
    return True


def _program(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def _git(where: Path, *args: str) -> str:
    """A git read in an in-place checkout (`deny_raw_git` forbids writes, not reads)."""
    return worktree_git(where, "-c", "core.fsmonitor=false", *args)


def _env() -> dict[str, str]:
    """This process's environment without the framework's own credentials (DF-294's rule):
    every `AGENTIC_*` variable but the plain settings. The person's own credentials stay -
    the repository's tool may need them to push."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("AGENTIC_") or name.upper() in _PLAIN_SETTINGS
    }
    return {**kept, "GIT_TERMINAL_PROMPT": "0"}


def _scrub(text: str) -> str:
    """URL credentials out of a tool's message (`https://user:token@host`)."""
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1<redacted>@", text)
