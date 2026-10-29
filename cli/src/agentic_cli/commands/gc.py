"""`agentic gc [--older-than DAYS] [--dry-run]` (M3-08): remove this repository's finished
runs - their run folders and trees - once older than the cut-off (`agentic_cli.housekeeping`
says which runs are kept, and why).

`--dry-run` lists what would go, with sizes, and changes nothing. A run that is kept is
not an error: gc exits 0 and lists each one with its reason.
"""

from typing import Annotated

import typer

from agentic_cli import housekeeping
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.workspace import Workspace


def register(app: typer.Typer) -> None:
    @app.command("gc")
    def gc(
        ctx: typer.Context,
        older_than: Annotated[
            str | None,
            typer.Option(
                "--older-than",
                metavar="DAYS",
                help="Finished runs older than this many days (default: gc.older_than_days "
                "in the user config, else the org policy, else 14).",
            ),
        ] = None,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="List what would be removed; remove nothing.")
        ] = False,
    ) -> None:
        """Remove finished runs (run folders and trees) older than a number of days."""
        out: Output = ctx.obj
        days = _days(older_than)
        ws = Workspace.load()
        settings = housekeeping.gc_settings(ws, days)
        candidates = housekeeping.plan_gc(ws, settings)
        outcome = housekeeping.collect(ws, candidates, dry_run=dry_run)
        for note in outcome.notes:
            out.warn(note)
        data = {
            "dry_run": dry_run,
            "older_than_days": settings.older_than_days,
            "older_than_source": settings.older_than_source,
            "removed": [c.as_dict() for c in outcome.removed],
            "kept": [c.as_dict() for c in outcome.kept],
            "freed_bytes": outcome.freed,
        }
        out.result(data, _show(data))


def _days(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        days = int(text)
    except ValueError:
        days = 0
    if days < 1:
        raise CliError(
            f"--older-than is a whole number of days, 1 or more, not {text!r}", ExitCode.USAGE
        )
    return days


def _show(data: dict):
    def show(console) -> None:
        verb = "would remove" if data["dry_run"] else "removed"
        cut = f"finished runs older than {data['older_than_days']} days"
        source = data["older_than_source"]
        removed, kept = data["removed"], data["kept"]
        size = housekeeping.human_bytes(data["freed_bytes"])
        if removed:
            console.print(f"{verb} {len(removed)} {cut} ({source}), {size}:", markup=False)
            for run in removed:
                when = (run["updated_at"] or "")[:10]
                line = f"  {run['run_id']}  {run['status']}  {when}  "
                console.print(line + housekeeping.human_bytes(run["bytes"]), markup=False)
        else:
            console.print(f"nothing to remove: no {cut} ({source}) can go", markup=False)
        if kept:
            console.print(f"kept {len(kept)}:", markup=False)
            for run in kept:
                console.print(f"  {run['run_id']}: {run['reason']}", markup=False)

    return show
