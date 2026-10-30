"""Org reports from the control plane (M3-01):

    agentic report chargeback [--month YYYY-MM] [--team T] [--output FILE]

`chargeback` fetches the month's cost per team (default: the previous calendar month) and
writes it as CSV: one line per team, the same bytes the control plane's CSV export gives.
The file is written whole or not at all and an existing file is never overwritten. A value
nobody measured is an empty cell, never 0. Calls the control plane cannot attribute to a run
are in `unattributed_usd` of their team (`(unattributed)` when they carry no team).

It needs org mode and `dashboard.view.org` (exec-viewer, auditor), or `dashboard.view.team`
in the team given with --team. Exit 0 written, 2 refused (permission, existing file, no org
mode), 64 a malformed month."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated

import typer

from agentic.orgapi.cost import MONTH_PATTERN, Chargeback, chargeback_csv
from agentic_cli import control_plane
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.output import table as output_table


def register(app: typer.Typer) -> None:
    group = typer.Typer(help="Org reports from the control plane.", no_args_is_help=True)
    app.add_typer(group, name="report")

    @group.command("chargeback")
    def chargeback(
        ctx: typer.Context,
        month: Annotated[
            str | None,
            typer.Option("--month", help="YYYY-MM. Default: the previous calendar month."),
        ] = None,
        team: Annotated[str | None, typer.Option("--team", help="One team only.")] = None,
        output: Annotated[
            Path | None,
            typer.Option("--output", "-o", help="Default: chargeback-<team|org>-<month>.csv."),
        ] = None,
    ) -> None:
        """The month's cost per team as CSV, for finance."""
        out: Output = ctx.obj
        if month is not None and not re.fullmatch(MONTH_PATTERN, month):
            raise CliError(f"--month {month!r} is not a month", ExitCode.USAGE, hint="YYYY-MM")
        if output is not None and output.exists():
            _refuse_existing(output)
        with org_client() as client:
            report = client.chargeback(month=month, team=team)
        target = output or Path(f"chargeback-{team or 'org'}-{report.month}.csv")
        write_csv(report, target)
        data = {
            "path": str(target),
            "month": report.month,
            "team": report.team,
            "teams": len(report.rows),
            "totals": report.totals.model_dump(mode="json"),
        }
        out.result(data, lambda console: _show(console, report, target))


def _refuse_existing(target: Path) -> None:
    raise CliError(
        f"{target} already exists",
        ExitCode.REFUSED,
        hint="pass another --output, or move the old file away",
    )


def write_csv(report: Chargeback, target: Path) -> None:
    """The report's CSV at `target`: written to `<target>.part`, flushed to disk, then moved
    into place, so a cut-off write leaves nothing behind; an existing file is refused."""
    if target.exists():
        _refuse_existing(target)
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)  # our own leftover from a write that was cut off
    try:
        with partial.open("xb") as sink:
            sink.write(chargeback_csv(report).encode("utf-8"))
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _usd(value) -> str:
    return "-" if value is None else f"${value}"


def _show(console, report: Chargeback, target: Path) -> None:
    shown = output_table("team", "users", "runs", "calls", "unattributed", "cost")
    for row in report.rows:
        shown.add_row(
            row.team,
            str(row.users),
            str(row.runs),
            str(row.calls),
            _usd(row.unattributed_usd),
            _usd(row.cost_usd),
        )
    console.print(shown)
    console.print(
        f"{report.month}: {len(report.rows)} team(s), total {_usd(report.totals.cost_usd)} "
        f"(unattributed {_usd(report.totals.unattributed_usd)}); written to {target}"
    )


def org_client() -> control_plane.ControlPlaneClient:
    home = agentic_home()
    settings = user_config(home)
    client = control_plane.client_for(os.environ, settings, home) if settings.get("org") else None
    if client is None:
        raise CliError(
            "org reports come from the control plane: this needs org mode with one",
            ExitCode.REFUSED,
            hint="set org and control_plane_url in ~/.agentic/config.yaml, then run "
            f"`{control_plane.LOGIN}`",
        )
    return client
