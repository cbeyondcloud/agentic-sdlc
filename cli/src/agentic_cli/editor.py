"""Opening a file in the person's editor (M1-55): `VISUAL`, else `EDITOR`, else Notepad on
Windows and `vi` elsewhere - the lookup order Git and Click use.

The editor value may carry arguments (`code --wait`, `"C:\\Program Files\\...\\x.exe" -w`):
it is split like a command line of the OS (POSIX rules on macOS and Linux, Windows
rules on Windows) and run without a shell, with the file as the last argument. A path
with spaces must be quoted, or it splits at the first space. On Windows the program is
looked up on PATH with PATHEXT first (`resolve_program`), so a shim such as `code.cmd`
starts as `code`.
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


def resolve_program(program: str, env: Mapping[str, str] | None = None) -> str:
    """On Windows, a bare program name as the file PATH holds for it, extensions from
    PATHEXT (`code` -> `...\\bin\\code.cmd`), which CreateProcess does not resolve. The
    current folder is never searched (a repository could plant `notepad.cmd` there).
    Elsewhere, and for a name with a folder in it, `program` is returned as it is."""
    if os.name != "nt" or os.path.dirname(program):
        return program
    env = os.environ if env is None else env
    extensions = [e for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    named = any(program.lower().endswith(e.lower()) for e in extensions)
    for folder in env.get("PATH", "").split(os.pathsep):
        if not folder.strip() or folder.strip() == ".":
            continue
        for name in [program] if named else [program + e for e in extensions]:
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    return program


def edit_file(path: Path) -> None:
    """Open `path` in the editor and wait until it is closed."""
    command = [*editor_command(), str(path)]
    program = resolve_program(command[0])
    try:
        done = subprocess.run([program, *command[1:]], check=False)
    except OSError as error:
        raise CliError(
            f"the editor could not be started ({command[0]}): {error}",
            ExitCode.ERROR,
            hint="set VISUAL or EDITOR to your editor (e.g. 'code --wait'); quote a path "
            "with spaces",
        ) from error
    if done.returncode != 0:
        raise CliError(
            f"the editor exited with code {done.returncode}; nothing was decided",
            ExitCode.ERROR,
        )
