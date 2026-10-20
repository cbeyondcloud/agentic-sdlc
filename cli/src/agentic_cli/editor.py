"""Opening a file in the person's editor (M1-55): `VISUAL`, else `EDITOR`, else Notepad on
Windows and `vi` elsewhere - the lookup order Git and Click use.

The editor value may carry arguments (`code --wait`, `"C:\\Program Files\\...\\x.exe" -w`):
it is split like a command line of the OS (POSIX rules on macOS and Linux, Windows
rules on Windows) and run without a shell, with the file as the last argument.
"""

import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode


def editor_command(env: Mapping[str, str] | None = None, *, windows: bool | None = None):
    env = os.environ if env is None else env
    windows = os.name == "nt" if windows is None else windows
    for key in ("VISUAL", "EDITOR"):
        value = env.get(key, "").strip()
        if value:
            parts = shlex.split(value, posix=not windows)
            return [_unquote(part) for part in parts] if windows else parts
    return ["notepad"] if windows else ["vi"]


def _unquote(part: str) -> str:
    if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
        return part[1:-1]
    return part


def edit_file(path: Path) -> None:
    """Open `path` in the editor and wait until it is closed."""
    command = [*editor_command(), str(path)]
    try:
        done = subprocess.run(command, check=False)
    except OSError as error:
        raise CliError(
            f"the editor could not be started ({command[0]}): {error}",
            ExitCode.ERROR,
            hint="set VISUAL or EDITOR to your editor (e.g. 'code --wait')",
        ) from error
    if done.returncode != 0:
        raise CliError(
            f"the editor exited with code {done.returncode}; nothing was decided",
            ExitCode.ERROR,
        )
