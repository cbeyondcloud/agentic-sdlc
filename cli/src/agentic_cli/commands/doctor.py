"""`agentic doctor` and `agentic support-bundle` (M3-09, spec §20).

    agentic doctor                         every check, each with a fix; exit 1 if one fails
    agentic support-bundle [--output ZIP]  a redacted zip for the platform team

`agentic_cli.doctor` says what is checked; `agentic_cli.support_bundle` what the bundle
holds and how nothing secret or personal gets into it.
"""

import os
from pathlib import Path

import typer

from agentic_cli import doctor as checks
from agentic_cli.config import agentic_home
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output


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


def run_checks() -> checks.DoctorReport:
    deps = checks.default_deps()
    if deps.gateway_transport is None:
        deps.gateway_transport = gateway_transport()
    return checks.run_doctor(Path.cwd(), agentic_home(), os.environ, deps)
