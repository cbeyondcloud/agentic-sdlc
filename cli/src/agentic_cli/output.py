"""Output conventions (M1-49).

* Results go to stdout; errors, warnings and progress to stderr, so a command's
  output can be piped or parsed.
* `--json` prints each result as one JSON document and each error as a JSON object
  (`{"error", "exit_code", "hint"}`) on stderr, for scripts and CI.
* Human output uses Rich; colour follows the terminal and `NO_COLOR` (Rich honours it),
  and `--no-color` turns it off.
"""

import json
from collections.abc import Callable
from typing import Any

from rich.console import Console

from agentic_cli.errors import CliError


class Output:
    def __init__(self, json_mode: bool = False, color: bool = True) -> None:
        self.json_mode = json_mode
        self.out = Console(no_color=not color, highlight=False, soft_wrap=True)
        self.err = Console(stderr=True, no_color=not color, highlight=False, soft_wrap=True)

    def result(self, data: Any, human: Callable[[Console], None] | None = None) -> None:
        """Show a command's result: `data` as JSON, or `human(console)` (default: str)."""
        if self.json_mode:
            print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
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
