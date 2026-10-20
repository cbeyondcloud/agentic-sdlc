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
"""

import json
import re
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.table import Table

from agentic_cli.errors import CliError

#: C0 controls except tab and newline, DEL, and C1 controls (CSI, OSC... as one byte).
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_C1 = re.compile(r"[\x7f-\x9f]")


def printable(text: str) -> str:
    """`text` with every terminal control character shown as U+FFFD, so an escape
    sequence cannot move the cursor, erase lines or retitle the window. Line ends stay."""
    return _CONTROLS.sub("�", text.replace("\r\n", "\n"))


class _Console(Console):
    """A Console that renders every string through `printable` (cells, rules, lines)."""

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

    def warn(self, message: str) -> None:
        if not self.json_mode:
            self.err.print(f"[yellow]warning:[/yellow] {message}")

    def error(self, error: CliError) -> None:
        if self.json_mode:
            body = {"error": str(error), "exit_code": int(error.code), "hint": error.hint}
            self.err.print(json.dumps(body, ensure_ascii=False), markup=False, highlight=False)
            return
        self.err.print(f"[red]error:[/red] {error}", markup=True)
        if error.hint:
            self.err.print(f"  {error.hint}", markup=False)


def _json(data: Any) -> str:
    """`data` as JSON; C0 controls are escaped by JSON itself, C1 controls here (as
    `\\u0080`...), so the text is the same for a parser and inert in a terminal."""
    text = json.dumps(data, indent=2, default=str, ensure_ascii=False)
    return _C1.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
