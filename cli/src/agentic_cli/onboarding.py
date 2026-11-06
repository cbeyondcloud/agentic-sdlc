"""The onboarding timer (M3-23): how long a developer takes from the first `agentic login`
to the first successful `agentic run --dry-run`, the developer guide's first-run path.

Nothing else on the machine says so: the keyring keeps no time, a dry run writes nothing,
and init's audit log is per repository. So `main` calls `note` after each command, and the
first try and first success of four steps are kept in `~/.agentic/onboarding.json`:

    login       `agentic login` (not `--for <host>`: that is the git host, a later step)
    init        `agentic init` (not `--repo`: the team lead's set-up of the repository)
    pipelines   `agentic pipelines list`
    dry-run     `agentic run <pipeline> --dry-run`

Times only: no command line, no path, no name. The file never leaves the machine (nothing
reads it but `agentic onboarding`), is not written in CI or with
`AGENTIC_ONBOARDING_TIMER=off`, and is not touched again once the dry run has passed.
A broken or unreadable file is ignored: the timer never fails a command.

Two commands finishing together read, change and replace the file under a lock
(`onboarding.json.lock`, at most `LOCK_WAIT_S` of waiting), so neither step is lost; a
failed write removes its temporary file, and a replace that Windows refuses because
another program (an antivirus scan, an indexer) holds the file is tried once more
(DF-996).
"""

import contextlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

from agentic.auth.credentials import CI_MARKERS
from agentic_cli.config import agentic_home

FILE = "onboarding.json"
OFF = "AGENTIC_ONBOARDING_TIMER"
STEPS = ("login", "init", "pipelines", "dry-run")
COMMANDS = {
    "login": "agentic login",
    "init": "agentic init",
    "pipelines": "agentic pipelines list",
    "dry-run": "agentic run <pipeline> --dry-run",
}
_GLOBAL = ("--json", "--no-color")
_FALSE = frozenset({"", "0", "false", "no", "off"})
#: The longest a command waits for another one's write (then its step is not recorded).
LOCK_WAIT_S = 2.0
#: The wait before the one retry of a replace another program held up (Windows).
_REPLACE_RETRY_S = 0.2


def step_of(args: Sequence[str]) -> str | None:
    """The onboarding step a command line is, or None."""
    words = [a for a in args if a not in _GLOBAL]
    if not words or any(w in ("-h", "--help", "--version") for w in words):
        return None
    command, flags = words[0], words[1:]
    if command == "login":
        return None if any(f == "--for" or f.startswith("--for=") for f in flags) else "login"
    if command == "init":
        return None if "--repo" in flags else "init"
    if command == "pipelines" and flags[:1] == ["list"]:
        return "pipelines"
    if command == "run" and "--dry-run" in flags:
        return "dry-run"
    return None


def note(
    args: Sequence[str],
    code: int,
    started: datetime,
    finished: datetime,
    *,
    home: Path | None = None,
    env: Mapping[str, str] = os.environ,
) -> None:
    """Keep the first try (`started`) and first success (`finished`, exit 0) of a step."""
    try:
        _note(args, code, started, finished, home, env)
    except BaseException:  # noqa: BLE001 - a timer never fails the command it times:
        pass  # not even Ctrl-C during its write (the command's own code is returned)


def _note(args, code, started, finished, home, env) -> None:
    step = step_of(args)
    if step is None or _disabled(env):
        return
    path = (home or agentic_home(env)) / FILE
    if _onboarded(_load(path)):
        return  # both ends recorded: the file (and its lock) is never touched again
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path.with_name(f"{path.name}.lock"), timeout=LOCK_WAIT_S):
        _record(path, step, code, started, finished)


def _onboarded(data: dict) -> bool:
    steps = data.get("steps", {})
    login, dry_run = steps.get("login") or {}, steps.get("dry-run") or {}
    return bool(login.get("first_try") and dry_run.get("first_ok"))


def _record(path: Path, step: str, code: int, started: datetime, finished: datetime) -> None:
    """Read, change and replace the file, under the lock: a step another command wrote
    since the first read is read again here, and kept."""
    data = _load(path)
    if _onboarded(data):
        return
    steps = data.setdefault("steps", {})
    changed = step not in steps
    entry = steps.setdefault(step, {"first_try": started.isoformat(), "first_ok": None})
    if code == 0 and not entry.get("first_ok"):
        entry["first_ok"], changed = finished.isoformat(), True
    if changed:
        _save(path, {"version": 1, "steps": steps})


def _disabled(env: Mapping[str, str]) -> bool:
    if env.get(OFF, "").strip().lower() in ("off", "0", "false", "no"):
        return True
    return any(env.get(name, "").strip().lower() not in _FALSE for name in CI_MARKERS)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
        return {}
    steps = {k: v for k, v in data["steps"].items() if k in STEPS and isinstance(v, dict)}
    return {**data, "steps": steps}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # one per process
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        try:
            os.replace(temporary, path)
        except PermissionError:  # Windows: another program holds the file for a moment
            time.sleep(_REPLACE_RETRY_S)
            os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def reset(home: Path | None = None) -> bool:
    """Forget the recorded steps (a rehearsal before the timed onboarding)."""
    path = (home or agentic_home()) / FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def report(home: Path | None = None) -> dict:
    """Each recorded step's time since the success just before it, whichever step that
    was (the first login: since its first try), so the steps add up to the total even when
    run out of order; and the total, from the first login try to the first successful dry
    run. The path is the human output's only: `--json` output gets pasted into issues."""
    path = (home or agentic_home()) / FILE
    steps = _load(path).get("steps", {})
    successes = sorted(t for t in (_time(e.get("first_ok")) for e in steps.values()) if t)
    rows, missing = [], None
    for step in STEPS:
        entry = steps.get(step) or {}
        ok = _time(entry.get("first_ok"))
        try_ = _time(entry.get("first_try"))
        if try_ is None and ok is None:
            missing = missing or step
            continue
        earlier = [t for t in successes if ok is not None and t < ok]
        since = earlier[-1] if earlier else try_
        rows.append(
            {
                "step": step,
                "command": COMMANDS[step],
                "first_try": entry.get("first_try"),
                "first_ok": entry.get("first_ok"),
                "seconds": _seconds(since, ok),
            }
        )
        if ok is None:
            missing = missing or step
    login = _time((steps.get("login") or {}).get("first_try"))
    done = _time((steps.get("dry-run") or {}).get("first_ok"))
    complete = login is not None and done is not None
    return {
        "complete": complete,
        "total_seconds": _seconds(login, done) if complete else None,
        "steps": rows,
        "next": None if missing is None else COMMANDS[missing],
        "file": str(path),
    }


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seconds(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, round((end - start).total_seconds()))


def shown_path(path: Path, user_home: Path | None = None) -> str:
    """`path` from `~` when it is under the home folder: the full path names the OS user."""
    base = user_home or Path.home()
    try:
        return "~/" + path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds // 60} min {seconds % 60} s"
