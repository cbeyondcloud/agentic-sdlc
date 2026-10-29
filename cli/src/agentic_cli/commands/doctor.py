"""`agentic doctor` and `agentic support-bundle` (M3-09, spec §20).

    agentic doctor                         every check, each with a fix; exit 1 if one fails
    agentic support-bundle [--output ZIP]  a redacted zip for the platform team

`agentic_cli.doctor` says what is checked; `agentic_cli.support_bundle` what the bundle
holds and how nothing secret or personal gets into it.
"""

import os
from pathlib import Path
from typing import Annotated

import typer

from agentic_cli import doctor as checks
from agentic_cli import support_bundle
from agentic_cli.config import agentic_home
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.workspace import Workspace


def gateway_transport():
    """The transport for the gateway check (None: real calls). Tests replace it."""
    return None


def register(app: typer.Typer) -> None:
    @app.command("doctor")
    def doctor(ctx: typer.Context) -> None:
        """Check this machine, its settings and the services a run needs."""
        out: Output = ctx.obj
        report = run_checks()
        out.result(report.to_json(), report.show)
        if not report.healthy:
            raise typer.Exit(ExitCode.ERROR)

    @app.command("support-bundle")
    def bundle(
        ctx: typer.Context,
        output: Annotated[
            Path | None,
            typer.Option(
                "--output",
                "-o",
                help="Where to write the zip (default: agentic-support-<time>.zip here).",
            ),
        ] = None,
    ) -> None:
        """Write a redacted zip for the platform team: versions, doctor, logs, run summaries."""
        out: Output = ctx.obj
        out.progress("running doctor...")
        report = run_checks()
        target = output or Path.cwd() / support_bundle.default_name(support_bundle.utc_now())
        try:
            ws = Workspace.load()
        except CliError:
            ws = None  # outside a repository: the machine's part only
        names = support_bundle.build(
            target, ws=ws, home=agentic_home(), env=os.environ, report=report
        )
        data = {"path": str(target), "contents": names, "healthy": report.healthy}

        def show(console) -> None:
            console.print(f"wrote {target} ({len(names)} files, redacted)", markup=False)
            console.print(
                "check it before you send it: unzip it, or open it in your file manager",
                markup=False,
            )

        out.result(data, show)


def run_checks() -> checks.DoctorReport:
    deps = checks.default_deps()
    if deps.gateway_transport is None:
        deps.gateway_transport = gateway_transport()
    return checks.run_doctor(Path.cwd(), agentic_home(), os.environ, deps)
