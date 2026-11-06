"""`agentic training-gateway`: recorded model answers for the training walkthrough (M3-23).

    agentic training-gateway [--port 8765]

Serves the walkthrough's cassette (`agentic_cli/training/cassettes`, recorded by the
acceptance scenario `test_accept_training_walkthrough.py`) with the eval cassette server
(`agentic.eval.replay`) in replay mode, on 127.0.0.1 only, until Ctrl-C. A run pointed at
it with `AGENTIC_GATEWAY_URL` gets the recorded answers in order: no model is called,
nothing leaves the machine, nothing is spent. It never forwards a call; one call more
than the recording is refused. Start it again for each new try of the walkthrough.

The port is one the system picks, unless `--port` names one (DF-998): then the
`AGENTIC_GATEWAY_URL` a trainee sets stays the same across restarts, and the walkthrough
can give one fixed line. A port another program holds is an error naming the fix.

Whether the trainee's pipeline is the recorded one shows in the pipeline step each call
names (its `X-Agentic-Step` header) against the recording's (`steps.json`, written with
the cassette). Request hashes cannot tell: they differ on every run (run ids, paths).
"""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from agentic.eval.replay import CassetteServer
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output

CASSETTES = Path(__file__).resolve().parent.parent / "training" / "cassettes"
KEY = "training/acceptance/1"  # the walkthrough scenario's cassette
STEPS = CASSETTES / "training" / "acceptance" / "steps.json"  # the step of each call


class TrainingServer(CassetteServer):
    """The cassette server, counting calls whose pipeline step is not the recording's."""

    def __init__(self, root: Path, expected: list[str]) -> None:
        super().__init__(root, "replay")
        self.expected = expected
        self.steps: list[str | None] = []

    def _answer(self, path: str, body: bytes, headers: dict[str, str]):
        with self._lock:
            self.steps.append(headers.get("x-agentic-step"))
        return super()._answer(path, body, headers)

    @property
    def differed(self) -> int:
        with self._lock:
            pairs = list(enumerate(self.steps))
        return sum(1 for i, step in pairs if i >= len(self.expected) or step != self.expected[i])


def _until_interrupted() -> None:
    while True:  # a sleep loop, not Event.wait: Ctrl-C reaches it on Windows too
        time.sleep(0.5)


wait: Callable[[], None] = _until_interrupted  # tests replace it


def register(app: typer.Typer) -> None:
    @app.command("training-gateway")
    def training_gateway(
        ctx: typer.Context,
        port: Annotated[
            int | None,
            typer.Option(
                min=1,
                max=65535,
                help="Serve on this port of 127.0.0.1 (default: one the system picks).",
            ),
        ] = None,
    ) -> None:
        """Serve the training walkthrough's recorded model answers (no model, no spend)."""
        out: Output = ctx.obj
        expected = json.loads(STEPS.read_text(encoding="utf-8"))
        try:
            server = TrainingServer(CASSETTES, expected).start(port=port or 0)
        except OSError as error:
            if not port:
                raise
            raise CliError(
                f"port {port} on 127.0.0.1 is in use by another program, or reserved by the "
                f"system ({error.strerror or error})",
                ExitCode.ERROR,
                hint="choose another port with --port, or leave --port out to let the "
                "system pick a free one",
            ) from None
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
        differed = server.differed  # answered, but for another step: another pipeline
        summary = {"gateway_url": url, "problems": problems, "differed": differed}
        message = "stopped" + (": " + "; ".join(problems) if problems else "")
        if differed:
            message += (
                f"\n{differed} requests differed from the recording (the training pipeline differs)"
            )
        out.result(summary, lambda c: c.print(message, markup=False))
