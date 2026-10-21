"""Output conventions (M1-49).

* Results go to stdout; errors, warnings and progress to stderr, so a command's
  output can be piped or parsed.
* `--json` prints each result as one JSON document and each error as a JSON object
  (`{"error", "exit_code", "hint"}`) on stderr, for scripts and CI.
* Human output uses Rich; colour follows the terminal and `NO_COLOR` (Rich honours it),
  and `--no-color` turns it off.
* Text from agents, repositories and providers (handoffs, diffs, logs, error reasons)
  never reaches the terminal as control sequences: every string Rich renders goes
  through `printable` (which `Syntax` and `Markdown` callers apply themselves), and
  JSON escapes the C1 controls a terminal would act on.
* Rich markup is off by default, so square brackets in a run id, path or reason print
  as they are (`[/x]` would raise MarkupError, `[bold]x` would vanish; DF-218). A styled
  line passes `markup=True` and puts any such text through `escape`.
* Non-UTF-8 streams (a Windows pipe or redirect expects the ANSI code page). What
  ships: Zyroflo's `tools` package, imported with the commands, reconfigures stdout
  and stderr to UTF-8 (errors replaced) in place, so human text goes out as UTF-8
  bytes and never crashes. `safe_streams` (called by `main`) is the backstop for a
  stream nothing reconfigured: it replaces what it cannot encode with `?`. JSON is
  ASCII-escaped whenever the stream was not UTF-8 when the process started
  (`agentic_cli.ORIGINAL_STREAMS`), so `agentic --json ... | ConvertFrom-Json` in
  Windows PowerShell gets non-ASCII names and paths exactly (DF-283).
"""

import json
import re
import sys
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from agentic_cli import ORIGINAL_STREAMS
from agentic_cli.errors import CliError

#: C0 controls except tab and newline, DEL, and C1 controls (CSI, OSC... as one byte).
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_C1 = re.compile(r"[\x7f-\x9f]")


def printable(text: str) -> str:
    """`text` with every terminal control character shown as U+FFFD, so an escape
    sequence cannot move the cursor, erase lines or retitle the window. Line ends stay."""
    return _CONTROLS.sub("�", text.replace("\r\n", "\n"))


class _Console(Console):
    """A Console that renders every string through `printable` (cells, rules, lines),
    with markup off unless a call turns it on."""

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=False, **kwargs)

    def render_str(self, text: str, **kwargs):
        return super().render_str(printable(text), **kwargs)


def table(*columns: str) -> Table:
    """A table whose cells wrap instead of being cut, and whose first column (the id or
    name a person copies into the next command) is never wrapped."""
    shown = Table()
    for index, column in enumerate(columns):
        shown.add_column(column, overflow="fold", no_wrap=index == 0)
    return shown


class Output:
    def __init__(self, json_mode: bool = False, color: bool = True) -> None:
        self.json_mode = json_mode
        self.out = _Console(no_color=not color, highlight=False, soft_wrap=True)
        self.err = _Console(stderr=True, no_color=not color, highlight=False, soft_wrap=True)

    def result(self, data: Any, human: Callable[[Console], None] | None = None) -> None:
        """Show a command's result: `data` as JSON, or `human(console)` (default: str)."""
        if self.json_mode:
            print(_json(data))
        elif human is not None:
            human(self.out)
        else:
            self.out.print(str(data), markup=False)

    def stream(self, data: Any, text: str) -> None:
        """Show one item of a stream as it arrives (`logs --follow`): with `--json`, one
        compact JSON document per line (JSON Lines); otherwise `text`, as it is."""
        if self.json_mode:
            print(_json(data, indent=None), flush=True)
        else:
            self.out.print(text, markup=False, end="")
            self.out.file.flush()

    def progress(self, message: str) -> None:
        """A note on stderr while a command works (not with --json: the result says it)."""
        if not self.json_mode:
            self.err.print(message, markup=False)

    def warn(self, message: str) -> None:
        if not self.json_mode:
            self.err.print(f"[yellow]warning:[/yellow] {escape(message)}", markup=True)

    def error(self, error: CliError) -> None:
        if self.json_mode:
            body = {"error": str(error), "exit_code": int(error.code), "hint": error.hint}
            text = json.dumps(body, ensure_ascii=not _exact("stderr"))
            self.err.print(text, markup=False, highlight=False)
            return
        self.err.print(f"[red]error:[/red] {escape(str(error))}", markup=True)
        if error.hint:
            self.err.print(f"  {error.hint}", markup=False)


def _json(data: Any, indent: int | None = 2) -> str:
    """`data` as JSON; C0 controls are escaped by JSON itself, C1 controls here (as
    `\\u0080`...), so the text is the same for a parser and inert in a terminal. On a
    stdout that is not UTF-8 everything beyond ASCII is escaped, so nothing is lost."""
    ascii_only = not _exact("stdout")
    text = json.dumps(data, indent=indent, default=str, ensure_ascii=ascii_only)
    return _C1.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _exact(name: str) -> bool:
    """Whether non-ASCII text reaches the reader of `sys.<name>` as it is: the stream is
    UTF-8 and, when it is still the stream the process started with, it was UTF-8 then
    too (an import may have reconfigured it in place; its reader did not change)."""
    stream = getattr(sys, name)
    original, encoding = ORIGINAL_STREAMS.get(name, (None, ""))
    if stream is original and encoding and not _utf8_name(encoding):
        return False
    return _utf8(stream)


def _utf8(stream: TextIO | None) -> bool:
    return _utf8_name(getattr(stream, "encoding", None) or "")


def _utf8_name(encoding: str) -> bool:
    return encoding.lower().replace("-", "").replace("_", "") in ("utf8", "utf8sig", "cp65001")


def safe_streams(*streams: TextIO | None) -> None:
    """Let each text stream that is not UTF-8 replace what it cannot encode (`?`)
    instead of raising UnicodeEncodeError mid-output: a Windows pipe or redirect uses
    the ANSI code page, and agent text holds arrows, accents and CJK. A UTF-8 stream,
    or one that cannot be reconfigured (a test capture), is left as it is."""
    for stream in streams or (sys.stdout, sys.stderr):
        if stream is None or _utf8(stream):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # a detached or closed stream
                continue
