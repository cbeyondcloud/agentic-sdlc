"""The `agentic` command (M1-49): global options, the command registry, and how
failures become exit codes (see `agentic exit-codes`).

    agentic [--json] [--no-color] <command> ...

Every command reports a failure by raising `CliError(message, code, hint)`; `main`
turns it into a message on stderr (or a JSON object with `--json`) and the code.
Anything else that escapes is exit 1 with its message and no traceback (set
`AGENTIC_DEBUG=1` to see the traceback); Ctrl-C is 130, and so are `kill <pid>`, a
closed terminal and Ctrl-Break, which stop a run the same way (its agent processes are
killed, M2-22); a wrong command or flag is 64, not Click's 2, because 2 means "refused
by a safety check".
"""

import os
import signal
import sys
import traceback
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

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
        with stop_signals_as_interrupts():
            return _run(command, args, out)
    except KeyboardInterrupt:  # a stop signal between commands' own handling
        out.err.print("interrupted", markup=False)
        return ExitCode.INTERRUPTED


def _run(command, args: list[str], out: Output) -> int:
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
        # argument) are the Click exceptions whose exit code is 2. Any other Click error
        # (a file parameter that cannot be opened) is exit 1, never a traceback (DF-252).
        message = error.format_message()
        if getattr(error, "exit_code", 1) != 2:
            out.error(CliError(message, ExitCode.ERROR))
            return ExitCode.ERROR
        ctx = getattr(error, "ctx", None)
        if out.json_mode:  # scripts parse stderr as JSON, usage errors included (DF-252)
            hint = ctx.get_usage() if ctx is not None else "see agentic --help"
            out.error(CliError(message, ExitCode.USAGE, hint=hint))
            return ExitCode.USAGE
        out.err.print(message, markup=False)
        if ctx is not None:
            out.err.print(ctx.get_usage(), markup=False)
        return ExitCode.USAGE
    except (KeyboardInterrupt, typer.Abort):
        out.err.print("interrupted", markup=False)
        return ExitCode.INTERRUPTED
    except Exception as error:  # noqa: BLE001 - the last line of defence, by design
        if os.environ.get("AGENTIC_DEBUG"):
            traceback.print_exc()
        out.error(CliError(f"{type(error).__name__}: {error}", ExitCode.ERROR))
        return ExitCode.ERROR


#: Signals that stop the CLI the way Ctrl-C does: `kill <pid>`, a closed terminal, and
#: Ctrl-Break on Windows (where each would otherwise end the process on the spot).
STOP_SIGNALS = ("SIGTERM", "SIGHUP", "SIGBREAK")


@contextmanager
def stop_signals_as_interrupts() -> Iterator[None]:
    """While open, a stop signal does what Ctrl-C does: inside `asyncio.run`, the
    runner's SIGINT handler cancels the running step cleanly (its spend is recorded, its
    process scope closes and kills everything it started, M2-22); elsewhere it raises
    KeyboardInterrupt - instead of the CLI dying with its agent processes left running.
    The previous handlers come back afterwards; outside the main thread nothing changes.
    A signal the parent ignored stays ignored: `nohup agentic run ...` must survive the
    hang-up it was started to survive."""
    previous = {}
    for name in STOP_SIGNALS:
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            if signal.getsignal(number) == signal.SIG_IGN:
                continue
            previous[number] = signal.signal(number, _interrupt)
        except (ValueError, OSError):  # not the main thread, or not catchable here
            pass
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _interrupt(signum: int, frame: object) -> None:
    handler = signal.getsignal(signal.SIGINT)
    if callable(handler):  # asyncio's (cancel the main task) or Python's default
        handler(signal.SIGINT, frame)
    else:
        raise KeyboardInterrupt


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())
