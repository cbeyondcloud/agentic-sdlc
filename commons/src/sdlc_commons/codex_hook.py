"""The guard as a Codex PreToolUse hook (M2-40, DF-116).

    python -I -m sdlc_commons.codex_hook <guard spec.json>

Codex runs this before each shell command (`Bash`) and each `apply_patch`, with the call
as JSON on stdin. The guard (`Guard`, Zyroflo's walls plus the framework's rules) judges
it, which gives Codex what its execpolicy lacks (spike S3 Q6):
  * a default-deny: a program no allow rule covers is refused;
  * redirect- and chain-aware matching: `echo ok > f && git push` is still a push;
  * write checks on patches: protected and denied paths, and the run's trees only.
An ask-listed command is refused too: a codex step cannot pause for a person.

Exit code 2 with the reason on stderr refuses the call; exit 0 with no output lets it
run. Codex treats a hook that crashes or prints malformed output as passed (it fails
open), so every error this module can catch - a missing or broken spec, unreadable
input, a guard failure - refuses. `-I` (isolated mode) keeps the current folder (the
agent's worktree) and PYTHON* variables out of the import path: a planted module there
cannot replace this one.

The spec file is written by the CLI when it builds a codex step (`write_spec`), outside
the worktree, and read here (`load_guard`).
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from sdlc_commons.guards import Guard

MODULE = "sdlc_commons.codex_hook"
_PATCH_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
_PATCH_MARKERS = frozenset({"*** Begin Patch", "*** End Patch", "*** End of File"})


def hook_argv(spec: Path) -> tuple[str, ...]:
    """The hook command for Codex: this interpreter, isolated, running this module."""
    return (sys.executable, "-I", "-m", MODULE, str(spec))


def write_spec(path: Path, guard: Guard, tree: Path) -> Path:
    """Save what rebuilds `guard` (for the step working in `tree`) to `path`."""
    spec = {
        "target": str(guard.target),
        "run_id": guard.run_id,
        "tree": str(tree),
        "deny_shell": list(guard.deny_shell),
        "deny_paths": list(guard.deny_paths),
        "allow_shell": None if guard.allow_shell is None else list(guard.allow_shell),
        "ask_shell": list(guard.ask_shell),
        "writes_files": guard.writes_files,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(spec, indent=2) + "\n").encode("utf-8"))
    return path


def load_guard(path: Path) -> tuple[Guard, Path]:
    """The guard and the step's tree, from a spec `write_spec` saved."""
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    guard = Guard(
        Path(spec["target"]),
        spec["run_id"],
        deny_shell=spec["deny_shell"],
        deny_paths=spec["deny_paths"],
        allow_shell=spec["allow_shell"],
        ask_shell=spec["ask_shell"],
        writes_files=spec["writes_files"],
    )
    return guard, Path(spec["tree"])


def patch_paths(patch: str) -> list[str]:
    """Every path an apply_patch text adds, updates, deletes or moves to.

    Codex trims each hunk header before matching it, so an indented header still writes
    (review I1): lines are read trimmed here too. That also reads an update hunk's
    context line that looks like a header as one - more paths checked, never fewer. A
    `*** ` line that is no marker this reader knows raises ValueError (the hook then
    refuses the patch): it may be one a later Codex writes through."""
    found = []
    for raw in patch.splitlines():
        line = raw.strip()
        header = next((h for h in _PATCH_HEADERS if line.startswith(h)), None)
        if header is not None:
            found.append(line[len(header) :].strip())
        elif line.startswith("*** ") and line not in _PATCH_MARKERS:
            raise ValueError("the patch has a marker the guard does not know")
    return found


def decide(call: dict, guard: Guard, tree: Path) -> str | None:
    """Why this tool call is refused, or None to let it run."""
    tool = call.get("tool_name")
    tool_input = call.get("tool_input")
    if not isinstance(tool, str):
        raise ValueError("the call names no tool")
    if tool not in ("Bash", "apply_patch"):
        return None
    if not isinstance(tool_input, dict):
        raise ValueError("the call has no tool_input object")
    texts = [value for value in tool_input.values() if isinstance(value, str)]
    if tool == "apply_patch":
        patch = next((text for text in texts if "*** Begin Patch" in text), None)
        if patch is None:
            raise ValueError("the patch call carries no patch")
        base = Path(call.get("cwd") or tree)
        paths = [str((base / path).resolve()) for path in patch_paths(patch)]
        blocks = guard.writes(paths)
        return "; ".join(block.why for block in blocks) or None
    command = tool_input.get("command")
    if not isinstance(command, str):
        raise ValueError("the shell call carries no command")
    blocks = guard.command(command)
    if blocks:
        return "; ".join(block.why for block in blocks)
    asks = guard.asks(command)
    if asks:
        return (
            f"the command needs a person's approval each time (ask rule {asks[0]!r}), and "
            "this step cannot pause for one (agent policy)"
        )
    return None


def main(
    argv: Sequence[str] | None = None, *, stdin: TextIO | None = None, stderr: TextIO | None = None
) -> int:
    """Read one PreToolUse call, answer 0 (run it) or 2 (refused, reason on stderr)."""
    args = list(sys.argv[1:] if argv is None else argv)
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    try:
        if len(args) != 1:
            raise ValueError("usage: python -I -m sdlc_commons.codex_hook <guard spec.json>")
        guard, tree = load_guard(Path(args[0]))
        call = json.loads(stdin.read())
        if not isinstance(call, dict):
            raise ValueError("the hook input is not a JSON object")
        reason = decide(call, guard, tree)
    except Exception as error:  # noqa: BLE001 - Codex runs a call whose hook failed
        stderr.write(f"refused: the guard could not check this call ({type(error).__name__})\n")
        return 2
    if reason:
        stderr.write(f"refused by the agentic guard: {reason}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
