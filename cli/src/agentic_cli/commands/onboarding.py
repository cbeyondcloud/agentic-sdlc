"""`agentic onboarding`: how long this machine's onboarding took (M3-23).

    agentic onboarding [--reset]

From the first `agentic login` to the first successful `agentic run --dry-run`, and each
step's time, read from `~/.agentic/onboarding.json` (see `agentic_cli.onboarding`). The
pilot's facilitator collects the total (docs/training/facilitator-sheet.md); nothing is
sent anywhere.
"""

from typing import Annotated

import typer
from rich.table import Table

from agentic_cli import onboarding
from agentic_cli.output import Output, printable

NOTHING_SENT = "Kept on this machine only; nothing is sent."


def register(app: typer.Typer) -> None:
    @app.command("onboarding")
    def show(
        ctx: typer.Context,
        reset: Annotated[
            bool, typer.Option("--reset", help="Forget the recorded steps and start again.")
        ] = False,
    ) -> None:
        """Time from your first `agentic login` to your first successful dry run."""
        out: Output = ctx.obj
        if reset:
            gone = onboarding.reset()
            message = "onboarding timer reset" if gone else "onboarding timer reset (nothing kept)"
            out.result({"reset": True}, lambda c: c.print(message, markup=False))
            return
        data = onboarding.report()
        out.result(data, lambda console: _show(console, data))


def _show(console, data: dict) -> None:
    if data["complete"]:
        total = onboarding.duration(data["total_seconds"])
        console.print(
            f"Onboarding took {total}: first `agentic login` to the first successful dry run.",
            markup=False,
        )
    elif not data["steps"]:
        console.print("No onboarding step is recorded on this machine yet.", markup=False)
    else:
        console.print(f"Not finished yet. Next: {data['next']}", markup=False)
    if data["steps"]:
        table = Table("step", "took", "first success")
        for row in data["steps"]:
            table.add_row(
                printable(row["command"]),
                onboarding.duration(row["seconds"]),
                printable(row["first_ok"] or "not yet"),
            )
        console.print(table)
    console.print(f"{NOTHING_SENT} ({printable(data['file'])})", markup=False)
