"""`agentic version` and `agentic exit-codes`."""

import typer
from rich.table import Table

from agentic_cli import __version__
from agentic_cli.exit_codes import EXIT_CODE_TABLE
from agentic_cli.output import Output


def register(app: typer.Typer) -> None:
    @app.command("version")
    def version(ctx: typer.Context) -> None:
        """Show the installed version."""
        out: Output = ctx.obj
        out.result({"version": __version__}, lambda c: c.print(f"agentic {__version__}"))

    @app.command("exit-codes")
    def exit_codes(ctx: typer.Context) -> None:
        """List the exit codes every command uses."""
        out: Output = ctx.obj

        def show(console) -> None:
            table = Table("code", "meaning")
            for code, meaning in EXIT_CODE_TABLE:
                table.add_row(str(int(code)), meaning)
            console.print(table)

        out.result({str(int(code)): meaning for code, meaning in EXIT_CODE_TABLE}, show)
