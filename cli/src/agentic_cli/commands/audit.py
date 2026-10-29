"""Proving the record is unchanged (M2-26, M2-13; spec §14.5):

    agentic audit verify [RUN_ID ...]          every local run ledger (or the ones named)
    agentic audit verify --control-plane       also the org's audit chain, checked here
    agentic audit verify --export FILE         an audit export file (repeatable)

Each problem names the run (or the record's seq), the file and entry, and the class of
change: `content` (edited after it was written), `hash` (its recorded hash replaced),
`link` (an entry removed, inserted, reordered, or changed and re-hashed), `sequence` (a
seq out of order) or `unreadable`. Exit 0 when everything verifies, 3 when a chain does
not, 1 when something could not be read, 2 when the control plane refused.

Local runs are checked when run ids are given, or when neither --control-plane nor
--export is. The control plane's chain needs `audit.read.org` and org mode
(`agentic login --for control-plane`)."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from agentic.auditproof import ExportReport
from agentic_cli import audit, control_plane
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.workspace import find_repo


def register(app: typer.Typer) -> None:
    group = typer.Typer(
        help="Verify run ledgers and the org audit chain; export audit records.",
        no_args_is_help=True,
    )
    app.add_typer(group, name="audit")

    @group.command("verify")
    def verify(
        ctx: typer.Context,
        run_ids: Annotated[
            list[str] | None, typer.Argument(metavar="[RUN_ID]...", help="Default: every run.")
        ] = None,
        with_control_plane: Annotated[
            bool,
            typer.Option("--control-plane", help="Also verify the org's audit chain."),
        ] = False,
        exports: Annotated[
            list[Path] | None,
            typer.Option("--export", help="Verify an audit export file (repeatable)."),
        ] = None,
    ) -> None:
        """Verify hash chains and say where any change was made."""
        out: Output = ctx.obj
        wanted = list(run_ids or [])
        local = bool(wanted) or not (with_control_plane or exports)
        runs = audit.verify_runs(find_repo(Path.cwd()), wanted) if local else []
        chain = None
        if with_control_plane:
            with org_client() as client:
                chain = audit.verify_control_plane(client, client.me().tenant.slug)
        files = audit.verify_exports(exports or [])
        intact = (
            all(r.intact for r in runs)
            and (chain is None or chain.intact)
            and all(f.ok for f in files)
        )
        data = {
            "intact": intact,
            "runs": [r.as_dict() for r in runs],
            "control_plane": None if chain is None else chain.as_dict(),
            "exports": [f.as_dict() for f in files],
        }

        def show(console) -> None:
            if local and not runs:
                console.print("No runs in this repository.")
            for report in runs:
                verdict = "intact" if report.intact else "BROKEN"
                if report.active:  # DF-618: a running run's last line is still being written
                    verdict += " (active: checked to its last whole event)"
                console.print(f"run {report.run_id}: {verdict} ({report.entries} events)")
                for f in report.findings:
                    console.print(f"  {_located(f)}", markup=False)
                for problem in report.problems:
                    console.print(f"  ledger check: {problem}", markup=False)
            if chain is not None:
                verdict = "intact" if chain.intact else "BROKEN"
                console.print(
                    f"control plane audit chain (tenant {chain.tenant}): {verdict} "
                    f"({chain.records} records)",
                    markup=False,
                )
                for f in chain.findings:
                    console.print(f"  {_located(f)}", markup=False)
            for report in files:
                show_export(console, report)

        out.result(data, show)
        if not intact:
            raise typer.Exit(ExitCode.INVALID)

    @group.command("export")
    def export(
        ctx: typer.Context,
        team: Annotated[
            str | None, typer.Option("--team", help="One team's records (others as links).")
        ] = None,
        start: Annotated[
            str | None,
            typer.Option("--from", help="ISO date, or date-time with a zone; inclusive."),
        ] = None,
        end: Annotated[
            str | None,
            typer.Option("--to", help="ISO date (that whole day), or date-time; exclusive."),
        ] = None,
        fmt: Annotated[
            ExportFormat, typer.Option("--format", help="jsonl or csv (both carry the proof).")
        ] = ExportFormat.jsonl,
        output: Annotated[
            Path | None,
            typer.Option("--output", "-o", help="Default: audit-<team>-<from>-<to>.<format>."),
        ] = None,
    ) -> None:
        """Export audit records with their chain proof (auditors), and verify the file."""
        out: Output = ctx.obj
        params = {"format": fmt.value}
        params.update({k: v for k, v in (("team", team), ("from", start), ("to", end)) if v})
        target = output or Path(audit.export_name(team, start, end, fmt.value))
        with org_client() as client:
            report = audit.download_export(client, params, target)
        data = report.as_dict()
        out.result(data, lambda console: show_export(console, report))
        if not report.ok:
            raise typer.Exit(ExitCode.INVALID)


class ExportFormat(StrEnum):
    jsonl = "jsonl"
    csv = "csv"


def _located(finding: dict) -> str:
    seq = f" (seq {finding['seq']})" if finding["seq"] is not None else ""
    if finding["file"] == "audit chain":
        where = f"seq {finding['seq']}" if finding["seq"] is not None else "record"
    else:
        where = f"{finding['file']} entry {finding['entry']}{seq}"
    return f"{where}: {finding['field_class']} - {finding['detail']}"


def _anchor(pair: tuple[int, str] | None) -> str:
    return "-" if pair is None else f"seq {pair[0]} {pair[1] or '(start of chain)'}"


def show_export(console, report: ExportReport) -> None:
    verdict = "intact" if report.ok else "BROKEN"
    scope = f"team {report.team}" if report.team else "whole tenant"
    console.print(
        f"{report.path}: {verdict} ({report.format}, {scope}, {report.entries} records, "
        f"{report.links} links)",
        markup=False,
    )
    console.print(
        f"  start {_anchor(report.start)}; end {_anchor(report.end)}"
        + (f"; head {_anchor(report.head)}" if report.head else ""),
        markup=False,
    )
    for finding in report.findings:
        console.print(f"  {finding}", markup=False)


def org_client() -> control_plane.ControlPlaneClient:
    home = agentic_home()
    settings = user_config(home)
    client = control_plane.client_for(os.environ, settings, home) if settings.get("org") else None
    if client is None:
        raise CliError(
            "the org audit chain lives in the control plane: this needs org mode with one",
            ExitCode.REFUSED,
            hint="set org and control_plane_url in ~/.agentic/config.yaml, then run "
            f"`{control_plane.LOGIN}`",
        )
    return client
