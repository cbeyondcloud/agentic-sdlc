"""Driving the engine from a command, and showing what the run came to (M1-54, M1-55).

* `drive`        - run one engine call with console logging through the redactor
                   (DF-175) and the run's spans into its folder (DF-165);
* `engine_errors` - the engine's error families as messages and exit codes (DF-54);
* `exit_code_for` - the run's state as the command's exit code: completed 0, waiting
                   for a person 4, stopped, killed or paused at a limit 5, failed 1;
* `summary` / `show_state` - the run as JSON, and as text with what to do next; the
                   free text in them (pause reasons, step details, why it ended) goes
                   through the redactor once more, for runs recorded before the engine
                   redacted what it stores.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from agentic.agents.runner import ResumeError
from agentic.definitions import DefinitionError, UnknownDefinitionError
from agentic.engine.engine import FAILING_PAUSE, GATEWAY_KEY_PAUSE, TERMINAL, EngineError
from agentic.engine.expressions import ExpressionError
from agentic.engine.gates import GateRefused
from agentic.engine.state import RunState, RunStateError
from agentic.killswitch import HOLD_PAUSE, UNCONFIRMED_PAUSE
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.logsetup import console_logging
from agentic_cli.output import Output
from agentic_cli.runtime import tracing_wanted
from sdlc_commons.ledger import LedgerError

#: Pauses at a limit (spec §8.4): budget, run time, a step's turns, time or stuck agent.
LIMIT_PAUSES = ("budget", "run-timeout", "step-budget", "step-turns", "step-time", "step-stuck")


@contextmanager
def engine_errors() -> Iterator[None]:
    try:
        yield
    except (EngineError, GateRefused, ResumeError) as error:
        raise CliError(str(error), ExitCode.REFUSED, hint=_hint(str(error))) from error
    except RunStateError as error:
        hint = "`agentic runs --all` lists this repository's runs"
        raise CliError(str(error), ExitCode.ERROR, hint=hint) from error
    except (DefinitionError, UnknownDefinitionError, ExpressionError) as error:
        raise CliError(str(error), ExitCode.INVALID) from error
    except LedgerError as error:
        raise CliError(f"the run ledger refused: {error}", ExitCode.ERROR) from error


def _hint(message: str) -> str | None:
    if "approve, reject or edit" in message:
        return "decide with `agentic approve`, `agentic reject --feedback` or `agentic edit`"
    if "busy" in message:
        return "wait for the other command to finish"
    if "exit condition" in message:  # an exhausted loop is not approved as it stands
        return (
            "`agentic edit` the pinned handoff (mark a review finding accepted, or name a "
            "failing test in the test report's accepted_failures and set its verdict to pass), "
            "`agentic reject --feedback` for another pass, or `agentic stop`"
        )
    return None


def drive(
    redact: Callable[[str], str], run_dir: Path, call: Callable[[], Awaitable[RunState]]
) -> RunState:
    try:
        with console_logging(redact), traces_into(run_dir), engine_errors():
            return asyncio.run(call())
    except CliError as error:  # an engine refusal can quote a provider's text
        raise CliError(redact(str(error)), error.code, hint=error.hint) from error


def show_policy_change(out: Output, run_id: str, moved: tuple[str, str] | None) -> None:
    """A paused standalone or `--policy` run continuing on its owner's edited policy
    (`Runtime.check_same_policy`, DF-697): say so before it runs."""
    if moved is None:
        return
    old, new = moved
    out.notice(
        {"policy": {"run_id": run_id, "from": old, "to": new}},
        f"run {run_id} continues on policy {new} (it ran on {old} until now); "
        "its ledger records the change",
    )


def exit_code_for(state: RunState) -> ExitCode:
    if state.status == "completed":
        return ExitCode.OK
    if state.status == "failed":
        return ExitCode.ERROR
    if state.status in ("stopped", "killed"):
        return ExitCode.STOPPED
    if state.pending is not None and state.pending.id in LIMIT_PAUSES:
        return ExitCode.STOPPED
    return ExitCode.PAUSED


def spent(state: RunState) -> Decimal:
    """What the run has spent: its budget snapshot, open slices included."""
    budget = state.budget or {}

    def total(node: dict) -> Decimal:
        own = Decimal(str(node.get("spent", "0")))
        return own + sum((total(c) for c in node.get("children", [])), Decimal(0))

    return total(budget) if budget else Decimal(0)


def summary(state: RunState, redact: Callable[[str], str] | None = None) -> dict:
    pending, clean = state.pending, redact or _as_is
    return {
        "run_id": state.run_id,
        "pipeline": state.pipeline,
        "pipeline_version": state.pipeline_version,
        "policy_version": state.policy_version,  # spec §1.3: the policy the run used
        "status": state.status,
        "regime": state.regime,
        "starter": state.starter,
        "started_at": state.started_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
        "sandbox": state.sandbox,
        "pending": None
        if pending is None
        else {
            "kind": pending.kind,
            "id": pending.id,
            "reason": clean(pending.reason),
            "step": pending.step,
            "since": pending.since.isoformat(),
            "artefacts": sorted(pending.artefacts),
            "calls": pending.calls,
        },
        "history": [
            {
                "step": r.step,
                "iteration": r.iteration,
                "outcome": r.outcome,
                "cost_usd": r.cost_usd,
                "handoff": r.handoff,
                "detail": clean(r.detail),
            }
            for r in state.history
        ],
        "cost_usd": str(spent(state)),
        "ended_by": state.ended_by,
        "end_reason": clean(state.end_reason) if state.end_reason else state.end_reason,
        "exit_code": int(exit_code_for(state)),
    }


def _as_is(text: str) -> str:
    return text


def next_steps(state: RunState) -> list[str]:
    pending, run = state.pending, state.run_id
    if state.status in TERMINAL:
        return []
    if pending is None:
        return [f"agentic resume {run}"]
    if pending.kind == "gate" and pending.id == f"{pending.step}-exhausted":
        # an exhausted loop (the engine names its gate after the loop): a bare approval is
        # refused until its exit condition holds, so edit and reject come first (DF-321)
        return [
            f"agentic show {run}",
            f"agentic edit {run}   (or: agentic reject {run} --feedback '...', "
            f"agentic stop {run} --reason ...; agentic approve {run} once its exit "
            "condition holds)",
        ]
    if pending.kind == "gate":
        return [
            f"agentic show {run}",
            f"agentic approve {run}   (or: agentic reject {run} --feedback '...', "
            f"agentic edit {run})",
        ]
    if pending.kind == "approval":
        calls = " ".join(f"--allow {c}" for c in pending.calls)
        return [f"agentic resume {run} {calls}   (or --deny <call>)"]
    if pending.id == "budget":  # the run's limit: more run budget lets it go on
        return [f"agentic resume {run} --extra-budget <USD>", f"agentic stop {run} --reason ..."]
    if pending.id == "step-budget":  # the agent's own limit: more run budget does not help
        return [
            f"agentic stop {run} --reason ...",
            f"agentic resume {run}   (retries the step from its start with the same step "
            "budget; a larger limits.budget_usd in the agent applies to new runs)",
        ]
    if pending.id == "run-timeout":
        return [f"agentic resume {run} --extra-minutes <N>", f"agentic stop {run} --reason ..."]
    if pending.id == "condition":
        return [f"agentic resume {run} --skip", f"agentic stop {run} --reason ..."]
    if pending.id == HOLD_PAUSE:  # a pause switch: resume continues once it is cleared
        return [
            f"agentic resume {run}   (once the pause is cleared; `agentic killswitch list` "
            "shows it)",
            f"agentic stop {run} --reason ...",
        ]
    if pending.id == UNCONFIRMED_PAUSE:  # the control plane could not be reached
        return [
            f"agentic resume {run}   (once the control plane is reachable again)",
            f"agentic stop {run} --reason ...",
        ]
    if pending.id == GATEWAY_KEY_PAUSE:  # the gateway refused the key (M2-17)
        return [
            "agentic login   (paste the new gateway key; or `agentic login --invite <link>` "
            "from a new invite; ask your org admin if you have neither)",
            f"agentic resume {run}",
        ]
    if pending.id == FAILING_PAUSE:  # resume would only pause again (DF-238)
        return [f"agentic show {run}", f"agentic stop {run} --reason ..."]
    return [f"agentic resume {run}", f"agentic stop {run} --reason ..."]


def show_state(state: RunState, redact: Callable[[str], str] | None = None):
    def show(console) -> None:
        data = summary(state, redact)
        console.print(
            f"run {state.run_id} ({state.pipeline} {state.pipeline_version}): {state.status}",
            markup=False,
        )
        console.print(f"  policy {state.policy_version}", markup=False)
        for record in data["history"]:
            detail = f" - {record['detail']}" if record["detail"] else ""
            console.print(
                f"  {record['step']} #{record['iteration']}: {record['outcome']} "
                f"(${record['cost_usd']}){detail}",
                markup=False,
            )
        pending = state.pending
        if pending is not None:
            where = "waiting at" if pending.kind == "gate" else "paused"
            reason = data["pending"]["reason"]
            console.print(f"  {where} {pending.id}: {reason}", markup=False)
        if state.end_reason:
            verb = "killed" if state.status == "killed" else "stopped"
            console.print(f"  {verb} by {state.ended_by}: {data['end_reason']}", markup=False)
        console.print(f"  spent ${data['cost_usd']}", markup=False)
        for line in next_steps(state):
            console.print(f"  next: {line}", markup=False)

    return show


# ── traces (DF-165) ──────────────────────────────────────────────────────────

_TRACING: dict = {}


@contextmanager
def traces_into(run_dir: Path) -> Iterator[None]:
    """While the context is open, finished spans go to `<run_dir>/traces.jsonl`. One
    tracer provider serves the process (OpenTelemetry allows one); a provider someone
    else installed is left alone."""
    if not tracing_wanted():
        yield
        return
    from opentelemetry import trace

    from agentic.telemetry.sdk import JsonlSpanExporter, install_tracing

    if "provider" not in _TRACING:
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            yield
            return
        exporter = _RunExporter()
        _TRACING.update(provider=install_tracing(exporter), exporter=exporter)
    exporter = _TRACING["exporter"]
    exporter.target = JsonlSpanExporter(Path(run_dir) / "traces.jsonl")
    try:
        yield
    finally:
        _TRACING["provider"].force_flush()
        exporter.target = None


class _RunExporter:
    """Hands spans to the current run's exporter (one run per command)."""

    def __init__(self) -> None:
        self.target = None

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        if self.target is None:
            return SpanExportResult.SUCCESS
        return self.target.export(spans)

    def shutdown(self) -> None:
        self.target = None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
