"""What a run cost (M1-57; spec §24 daily loop, §26):

    agentic cost <run>      by step, agent and child, from the run ledger
    agentic cost            the total of each run in this repository

The run ledger's `step_finished` events are the record: each carries the step's spend
and, when it spawned children, each child's (`cost.children`). Their sum is the total.
The run's budget snapshot (`run.json`) is shown beside it; any difference (spend no
step recorded, e.g. a call cut off by a crash) is the `unattributed` line, never hidden.
Amounts are exact decimals, as text in JSON.
"""

from decimal import Decimal
from typing import Annotated

import typer

from agentic.definitions.pipeline import ActionStep, AgentStep, LoopStep
from agentic.engine.state import RunState
from agentic_cli.commands.runs import _all_runs, _load
from agentic_cli.driving import spent
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.output import table as output_table
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons.ledger import LedgerError, RunLedger


def register(app: typer.Typer) -> None:
    @app.command("cost")
    def cost(ctx: typer.Context, run_id: Annotated[str | None, typer.Argument()] = None) -> None:
        """What a run cost, by step, agent and child (or every run's total)."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        if run_id is None:
            rows = [
                {"run_id": s.run_id, "pipeline": s.pipeline, "usd": str(spent(s))}
                for s in sorted(_all_runs(rt), key=lambda s: s.started_at)
            ]
            total = sum((Decimal(r["usd"]) for r in rows), Decimal(0))
            data = {"runs": rows, "total_usd": str(total)}
            out.result(data, _show_runs(data))
            return
        state = _load(rt, run_id)
        data = breakdown(rt, state)
        out.result(data, _show_run(data))


def breakdown(rt: Runtime, state: RunState) -> dict:
    ledger = RunLedger(rt.ws.repo, state.run_id)
    try:
        events = ledger.events()
        verified = not ledger.verify()
    except LedgerError as error:
        raise CliError(f"the run ledger cannot be read: {error}", ExitCode.ERROR) from error
    agents = _agents_by_step(rt, state)
    steps, by_agent, by_child = [], {}, {}
    total = Decimal(0)
    for event in events:
        if event.get("event") != "step_finished" or not isinstance(event.get("cost"), dict):
            continue
        step, usd = event.get("step", ""), Decimal(str(event["cost"].get("usd", "0")))
        children = {k: str(v) for k, v in (event["cost"].get("children") or {}).items()}
        agent = agents.get(step, "?")
        steps.append(
            {
                "step": step,
                "agent": agent,
                "outcome": event.get("outcome"),
                "usd": str(usd),
                "children": children,
            }
        )
        total += usd
        by_agent[agent] = by_agent.get(agent, Decimal(0)) + usd
        for child, amount in children.items():
            key = f"{step}/{child}"
            by_child[key] = by_child.get(key, Decimal(0)) + Decimal(amount)
    run_spent = spent(state)
    budget = state.budget or {}
    return {
        "run_id": state.run_id,
        "pipeline": state.pipeline,
        "status": state.status,
        "steps": steps,
        "by_agent": {k: str(v) for k, v in by_agent.items()},
        "by_child": {k: str(v) for k, v in by_child.items()},
        "total_usd": str(total),
        "run_spent_usd": str(run_spent),
        "unattributed_usd": str(run_spent - total),
        "unpriced_calls": int(budget.get("unpriced_calls", 0)),
        "unpriced_usd": str(budget.get("unpriced_usd", "0")),
        "ledger_verified": verified,
    }


def _agents_by_step(rt: Runtime, state: RunState) -> dict[str, str]:
    """Which agent (or action) each step id ran, from the run's pipeline."""
    try:
        pipeline = rt.ws.definitions().pipeline(state.pipeline)
    except Exception:  # a pipeline gone from the policy: the steps still show
        return {}
    found: dict[str, str] = {}
    for step in pipeline.steps:
        for inner in step.loop.steps if isinstance(step, LoopStep) else [step]:
            if isinstance(inner, AgentStep):
                found[inner.id] = inner.agent
            elif isinstance(inner, ActionStep):
                found[inner.id] = f"action:{inner.action}"
    return found


def _show_run(data: dict):
    def human(console) -> None:
        console.print(f"cost of run {data['run_id']} ({data['pipeline']}, {data['status']})")
        table = output_table("step", "agent", "outcome", "USD", "children")
        for s in data["steps"]:
            children = ", ".join(f"{k} ${v}" for k, v in s["children"].items())
            table.add_row(s["step"], s["agent"], str(s["outcome"]), f"${s['usd']}", children)
        console.print(table)
        agents = ", ".join(f"{k} ${v}" for k, v in data["by_agent"].items()) or "none"
        console.print(f"by agent: {agents}", markup=False)
        if data["by_child"]:
            children = ", ".join(f"{k} ${v}" for k, v in data["by_child"].items())
            console.print(f"by child: {children}", markup=False)
        console.print(f"total ${data['total_usd']} (ledger)", markup=False)
        if Decimal(data["unattributed_usd"]) != 0:
            console.print(
                f"run budget shows ${data['run_spent_usd']}: ${data['unattributed_usd']} "
                "is not attributed to a step",
                markup=False,
            )
        if data["unpriced_calls"]:
            console.print(
                f"{data['unpriced_calls']} call(s) had no reported cost and were charged "
                f"their estimate (${data['unpriced_usd']})",
                markup=False,
            )
        if not data["ledger_verified"]:
            console.print("warning: the run ledger does not verify", markup=False)

    return human


def _show_runs(data: dict):
    def human(console) -> None:
        if not data["runs"]:
            console.print("no runs", markup=False)
            return
        table = output_table("run", "pipeline", "USD")
        for r in data["runs"]:
            table.add_row(r["run_id"], r["pipeline"], f"${r['usd']}")
        console.print(table)
        console.print(f"total ${data['total_usd']}", markup=False)

    return human
