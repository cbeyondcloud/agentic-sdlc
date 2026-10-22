"""`agentic sync [RUN_ID]`: deliver run events that stayed queued to the control plane
(M2-20), for one run or every run of this repository.

The run ledger (`events.jsonl`) is the queue and the sink cursor its committed offset, so
syncing sends exactly what the control plane has not taken yet, in order; it de-duplicates
by (run, seq, hash), so syncing twice changes nothing. A run the control plane refuses (a
changed chain) is reported and left queued; the others still go."""

from typing import Annotated

import typer

from agentic.telemetry.sinks import ForwardingRecorder
from agentic_cli import control_plane
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.workspace import Workspace
from sdlc_commons.ledger import LedgerError, RunLedger, run_id_refusal


def register(app: typer.Typer) -> None:
    @app.command("sync")
    def sync(
        ctx: typer.Context,
        run_id: Annotated[str | None, typer.Argument(help="One run; default: every run.")] = None,
    ) -> None:
        """Send run events still queued for the control plane."""
        out: Output = ctx.obj
        ws = Workspace.load()
        client = control_plane.client_for(ws.env, ws.settings, ws.home)
        if client is None:
            raise CliError(
                "no control plane to sync with",
                ExitCode.INVALID,
                hint=f"set control_plane_url in ~/.agentic/config.yaml or {control_plane.URL_ENV}",
            )
        if run_id is not None and (why := run_id_refusal(run_id)):
            raise CliError(why, ExitCode.USAGE)
        runs = [run_id] if run_id else _run_ids(ws)
        results: dict[str, dict] = {}
        with client:
            sink = control_plane.ControlPlaneSink(client)
            for name in runs:
                results[name] = _sync_run(ws, sink, name)
        data = {"runs": results}

        def show(console) -> None:
            if not results:
                console.print("No runs to sync.")
            for name, result in results.items():
                line = f"{name}: sent {result['sent']}, queued {result['queued']}"
                if "error" in result:
                    line += f" ({result['error']})"
                console.print(line, markup=False)

        out.result(data, show)
        if any("error" in result for result in results.values()):
            raise typer.Exit(ExitCode.ERROR)


def _run_ids(ws: Workspace) -> list[str]:
    root = ws.runs_root
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "events.jsonl").is_file())


def _sync_run(ws: Workspace, sink: control_plane.ControlPlaneSink, run_id: str) -> dict:
    errors: list[str] = []
    try:
        ledger = RunLedger(ws.repo, run_id)
        if not ledger.events():
            return {"sent": 0, "queued": 0}
        recorder = ForwardingRecorder(
            ledger, [sink], on_error=lambda name, error: errors.append(str(error))
        )
        sent = recorder.flush().get(sink.name, 0)
        result = {"sent": sent, "queued": recorder.pending().get(sink.name, 0)}
    except LedgerError as error:
        return {"sent": 0, "queued": 0, "error": str(error)}
    if errors:
        result["error"] = errors[0]
    return result
