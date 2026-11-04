"""`agentic training-gateway`: recorded model answers for the training walkthrough (M3-23).

    agentic training-gateway

Serves the walkthrough's cassette (`agentic_cli/training/cassettes`, recorded by the
acceptance scenario `test_accept_training_walkthrough.py`) with the eval cassette server
(`agentic.eval.replay`) in replay mode, on 127.0.0.1 only, until Ctrl-C. A run pointed at
it with `AGENTIC_GATEWAY_URL` gets the recorded answers in order: no model is called,
nothing leaves the machine, nothing is spent. It never forwards a call; one call more
than the recording is refused. Start it again for each new try of the walkthrough.
"""

import time
from collections.abc import Callable
from pathlib import Path

import typer

from agentic.eval.replay import CassetteServer
from agentic_cli.output import Output

CASSETTES = Path(__file__).resolve().parent.parent / "training" / "cassettes"
KEY = "training/acceptance/1"  # the walkthrough scenario's cassette


def _until_interrupted() -> None:
    while True:  # a sleep loop, not Event.wait: Ctrl-C reaches it on Windows too
        time.sleep(0.5)


wait: Callable[[], None] = _until_interrupted  # tests replace it


def register(app: typer.Typer) -> None:
    @app.command("training-gateway")
    def training_gateway(ctx: typer.Context) -> None:
        """Serve the training walkthrough's recorded model answers (no model, no spend)."""
        out: Output = ctx.obj
        server = CassetteServer(CASSETTES, "replay").start()
        try:
            server.expect([KEY])
            url = server.base_url(KEY)
            ready = (
                "Training gateway ready: recorded answers, no model, nothing spent.\n"
                "In the terminal where you run agentic, set:\n"
                f"  export AGENTIC_GATEWAY_URL={url}\n"
                f'  (PowerShell: $env:AGENTIC_GATEWAY_URL = "{url}")\n'
                "Ctrl-C stops it; start it again for each new try."
            )
            if out.json_mode:
                out.notice({"gateway_url": url}, ready)
            else:  # never wrapped: the export line is copied as it is
                out.err.print(ready, markup=False, highlight=False, soft_wrap=True)
            try:
                wait()
            except KeyboardInterrupt:
                pass
            problems = server.finish()
        finally:
            server.close()
        summary = {"gateway_url": url, "problems": problems}
        message = "stopped" + (": " + "; ".join(problems) if problems else "")
        out.result(summary, lambda c: c.print(message, markup=False))
