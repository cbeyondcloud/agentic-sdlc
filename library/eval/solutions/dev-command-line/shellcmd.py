"""Show a command so a person can paste it into their shell."""

import shlex
import subprocess


def command_line(args: list, windows: bool) -> str:
    if windows:
        return subprocess.list2cmdline(args)
    return shlex.join(args)
