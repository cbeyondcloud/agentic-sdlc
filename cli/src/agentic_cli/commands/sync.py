"""`agentic sync [RUN_ID]`: deliver run events that stayed queued to the control plane
(M2-20), for one run or every run of this repository.

The run ledger (`events.jsonl`) is the queue and the sink cursor its committed offset, so
syncing sends exactly what the control plane has not taken yet, in order; it de-duplicates
by (run, seq, hash), so syncing twice changes nothing. A run the control plane refuses (a
changed chain) is reported and left queued; the others still go. A run the publish scan
holds back (M2-49: a chained event with a secret or personal data) is sent up to that event
and reported as held back, with who to ask: syncing again cannot send the rest."""

from typing import Annotated

import typer

from agentic.security.publish_scan import PublishBlocked
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
            guard = control_plane.publish_guard(ws.policy)  # M2-49: nothing unscanned leaves
            sink = control_plane.ControlPlaneSink(client, guard=guard)
            for name in runs:
                results[name] = _sync_run(ws, sink, name)
        data = {"runs": results}

        def show(console) -> None:
            if not results:
                console.print("No runs to sync.")
            for name, result in results.items():
                line = f"{name}: sent {result['sent']}, queued {result['queued']}"
                if "blocked" in result:
                    line += f", held back from event {result['blocked']['seq']}"
                if "error" in result:
                    line += f" ({result['error']})"
                console.print(line, markup=False)
                if "hint" in result:
                    console.print(f"  hint: {result['hint']}", markup=False)

        out.result(data, show)
        if any("error" in result for result in results.values()):
            raise typer.Exit(ExitCode.ERROR)


def _run_ids(ws: Workspace) -> list[str]:
    root = ws.runs_root
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "events.jsonl").is_file())


def _sync_run(ws: Workspace, sink: control_plane.ControlPlaneSink, run_id: str) -> dict:
    errors: list[Exception] = []
    try:
        ledger = RunLedger(ws.repo, run_id)
        if not ledger.events():
            return {"sent": 0, "queued": 0}
        recorder = ForwardingRecorder(
            ledger, [sink], on_error=lambda name, error: errors.append(error)
        )
        flushed = recorder.flush().get(sink.name, 0)
        result: dict = {"sent": flushed, "queued": recorder.pending().get(sink.name, 0)}
    except LedgerError as error:
        return {"sent": 0, "queued": 0, "error": str(error)}
    if errors:
        first = errors[0]
        result["error"] = str(first)
        if isinstance(first, PublishBlocked):  # M2-49: held back for good, not a retry case
            result["sent"] = first.delivered
            result["blocked"] = {"seq": first.seq}
            result["hint"] = (
                f"ask your org admin how to handle a run the publish scan held back; its "
                f"full record stays in {ledger.folder} and syncing again will not send it"
            )
    return result
