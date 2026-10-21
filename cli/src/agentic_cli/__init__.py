"""The agentic command-line tool."""

import sys
from importlib.metadata import version

__version__ = version("agentic-cli")

#: stdout and stderr as the process started, with their encodings, read before anything
#: else is imported: Zyroflo's `tools` package reconfigures both streams to UTF-8 in
#: place when it is imported, which hides that a Windows pipe or redirect expects the
#: ANSI code page (`agentic_cli.output` escapes JSON for such a stream, DF-283).
ORIGINAL_STREAMS: dict[str, tuple[object, str]] = {
    name: (getattr(sys, name), str(getattr(getattr(sys, name), "encoding", "") or ""))
    for name in ("stdout", "stderr")
}
