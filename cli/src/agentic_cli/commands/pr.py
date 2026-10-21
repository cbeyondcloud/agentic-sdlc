"""The run's pull request (M1-58; spec §16):

    agentic pr open <run>                                  push the branch, open or update
    agentic pr checks <run> [--watch] [--timeout S] [--interval S]     what CI says

A pipeline normally opens its pull request itself (`action: github.open_pr`); `pr open`
does it for a completed run that has no such step, or refreshes the body. `pr checks`
reads CI once, or with `--watch` until no check is still running; it exits 0 only when
CI is green - red, pending, no checks at all and unknown all exit 1 (Zyroflo's rule:
four of the five answers are not green). Nothing here merges: a person does.
"""

from typing import Annotated

import typer

from agentic.engine.state import RunState
from agentic_cli.driving import engine_errors
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.publishing import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    publish_run,
    run_checks,
)
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons.release import GREEN

pr_app = typer.Typer(help="The run's pull request: open it, read its CI.", no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(pr_app, name="pr")


@pr_app.command("open")
def open_pr(ctx: typer.Context, run_id: str) -> None:
    """Push a completed run's branch and open (or update) its pull request."""
    out: Output = ctx.obj
    rt = Runtime(Workspace.load())
    state = _load(rt, run_id)
    if state.status != "completed":
        raise CliError(
            f"run {run_id} is {state.status}, not completed",
            ExitCode.REFUSED,
            hint="finish it first (`agentic status` says what it waits for)",
        )
    record = publish_run(rt.ws, rt.store.runs_root, state)

    def show(console) -> None:
        for pull in record["pull_requests"]:
            verb = "Opened" if pull["created"] else "Updated"
            console.print(f"{verb} {pull['url']} ({pull['branch']} -> {pull['base']})")

    out.result(record, show)


@pr_app.command("checks")
def checks(
    ctx: typer.Context,
    run_id: str,
    watch: Annotated[
        bool, typer.Option("--watch", help="Wait until no check is still running.")
    ] = False,
    timeout: Annotated[
        int, typer.Option("--timeout", min=1, help="With --watch: give up after these seconds.")
    ] = DEFAULT_TIMEOUT_SECONDS,
    interval: Annotated[
        int, typer.Option("--interval", min=1, help="With --watch: seconds between reads.")
    ] = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """What CI says about the run's pull request (exit 0 only when green)."""
    out: Output = ctx.obj
    rt = Runtime(Workspace.load())
    _load(rt, run_id)
    result = run_checks(
        rt.ws, rt.store.runs_root, run_id, watch=watch, timeout=timeout, interval=interval
    )

    def show(console) -> None:
        console.print(f"CI: {result['state']} - {result['why']}", markup=False)
        if result.get("timed_out"):
            console.print(f"  gave up waiting after {timeout}s", markup=False)
        failing = set(result["failed"]) | set(result["pending"])
        for row in result["rows"]:
            if row["name"] in failing:
                link = f" {row['link']}" if row["link"] else ""
                console.print(f"  {row['name']}: {row['conclusion'] or row['status']}{link}")

    out.result(result, show)
    if result["state"] != GREEN:
        raise typer.Exit(ExitCode.ERROR)


def _load(rt: Runtime, run_id: str) -> RunState:
    with engine_errors():
        return rt.store.load(run_id)
