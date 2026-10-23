"""Test helper: a stand-in for `srt` (never the real sandbox), so a step wrapped by
`StepWiring.process_wrap_for` runs on every OS. Run as

    python _fake_srt.py --settings <file> -- env NAME=value ... <program> <args>

It appends the settings it was given and the command to the JSONL file named by
FAKE_SRT_LOG, applies the `env NAME=value` prefix itself (Windows has no `env`), then runs
the command with stdin and stdout passed straight through, and exits with its code."""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    settings = Path(args[args.index("--settings") + 1])
    command = args[args.index("--") + 1 :]
    env = dict(os.environ)
    if command and command[0] == "env":
        command = command[1:]
        while command and "=" in command[0] and not command[0].startswith("-"):
            name, _, value = command.pop(0).partition("=")
            env[name] = value
    log = os.environ.get("FAKE_SRT_LOG")
    if log:
        record = {"settings": json.loads(settings.read_text(encoding="utf-8")), "argv": command}
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main())
