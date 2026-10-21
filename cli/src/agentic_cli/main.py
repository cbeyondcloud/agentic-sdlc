"""The `agentic` command (M1-49): global options, the command registry, and how
failures become exit codes (see `agentic exit-codes`).

    agentic [--json] [--no-color] <command> ...

Every command reports a failure by raising `CliError(message, code, hint)`; `main`
turns it into a message on stderr (or a JSON object with `--json`) and the code.
Anything else that escapes is exit 1 with its message and no traceback (set
`AGENTIC_DEBUG=1` to see the traceback); Ctrl-C is 130; a wrong command or flag is
64, not Click's 2, because 2 means "refused by a safety check".
"""

import os
import sys
import traceback
from collections.abc import Sequence

import typer

from agentic_cli.commands import COMMANDS
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output, safe_streams


def build_app() -> typer.Typer:
    app = typer.Typer(
        name="agentic",
        help="Run governed AI agent pipelines on your repository.",
        no_args_is_help=True,
        add_completion=False,
        pretty_exceptions_enable=False,
        context_settings={"help_option_names": ["-h", "--help"]},
    )

    @app.callback()
    def options(
        ctx: typer.Context,
        json_mode: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
        no_color: bool = typer.Option(False, "--no-color", help="No colour in the output."),
    ) -> None:
        ctx.obj = Output(json_mode=json_mode, color=not no_color)

    for register in COMMANDS:
        register(app)
    return app


def main(argv: Sequence[str] | None = None, app: typer.Typer | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    safe_streams()  # a Windows pipe is not UTF-8: never crash on an arrow in agent text
    command = typer.main.get_command(app or build_app())
    out = Output(json_mode="--json" in args, color="--no-color" not in args)
    try:
        # Typer returns Click's exit code instead of raising for Ctrl-C (130)
        returned = command.main(args=args, prog_name="agentic", standalone_mode=False)
        return returned if isinstance(returned, int) else ExitCode.OK
    except CliError as error:
        out.error(error)
        return error.code
    except typer.Exit as done:  # --help, or a command's explicit exit
        return done.exit_code
    except typer.TyperException as error:
        # Typer vendors Click; its usage errors (unknown command, bad flag, missing
        # argument) are the Click exceptions whose exit code is 2.
        if getattr(error, "exit_code", 1) != 2:
            raise
        out.err.print(error.format_message(), markup=False)
        if getattr(error, "ctx", None) is not None:
            out.err.print(error.ctx.get_usage(), markup=False)
        return ExitCode.USAGE
    except (KeyboardInterrupt, typer.Abort):
        out.err.print("interrupted", markup=False)
        return ExitCode.INTERRUPTED
    except Exception as error:  # noqa: BLE001 - the last line of defence, by design
        if os.environ.get("AGENTIC_DEBUG"):
            traceback.print_exc()
        out.error(CliError(f"{type(error).__name__}: {error}", ExitCode.ERROR))
        return ExitCode.ERROR


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())
