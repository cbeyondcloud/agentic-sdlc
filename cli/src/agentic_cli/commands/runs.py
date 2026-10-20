"""Starting and following runs (M1-54; spec §8.2, §8.3, §24 steps 6-8):

    agentic run <pipeline> [--input k=v]... [--budget USD] [--run-id ID] [--dry-run]
    agentic resume <run> [--extra-minutes N] [--extra-budget USD] [--allow CALL]...
                         [--deny CALL]... [--skip]
    agentic status [<run>]
    agentic runs [--all]
    agentic stop <run> --reason TEXT
    agentic logs <run> [--step ID] [--level LEVEL]          (M1-47's command)

A run that waits for a person exits 4, one stopped (or paused at a limit) exits 5, a
dry run whose preflight fails exits 3 with the report.
"""

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Annotated

import typer

from agentic.engine.state import RunState, RunStateError
from agentic.telemetry.logs import LEVELS, read_logs, render_logs
from agentic_cli.driving import (
    drive,
    engine_errors,
    exit_code_for,
    show_state,
    spent,
    summary,
)
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.output import table as output_table
from agentic_cli.runtime import Runtime, new_run_id
from agentic_cli.workspace import Workspace


def register(app: typer.Typer) -> None:
    @app.command("run")
    def run(
        ctx: typer.Context,
        pipeline: str,
        inputs: Annotated[
            list[str] | None, typer.Option("--input", help="key=value, as the pipeline declares")
        ] = None,
        budget: Annotated[str | None, typer.Option(help="Run budget in USD.")] = None,
        run_id: Annotated[str | None, typer.Option("--run-id", help="Name the run.")] = None,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Preflight and plan only: nothing is spent.")
        ] = False,
    ) -> None:
        """Run a pipeline (or show what it would do, with --dry-run)."""
        out: Output = ctx.obj
        given = _inputs(inputs or [])
        rt = Runtime(Workspace.load())
        if dry_run:
            report = rt.preflight(pipeline, live=False)
            plan = rt.plan(pipeline, report)
            out.result(plan, _show_plan(plan))
            raise typer.Exit(ExitCode.OK if report.passed else ExitCode.INVALID)
        limit = _usd(budget, "--budget")
        gateway = rt.gateway()  # before anything is installed or written
        report = rt.preflight(pipeline, live=True)
        if not report.passed:
            out.result({"preflight": report.text()}, lambda c: c.print(report.text(), markup=False))
            raise CliError("preflight failed: nothing was started", ExitCode.INVALID)
        for problem in report.problems:
            out.warn(f"{problem.step or 'pipeline'}: {problem.check}: {problem.detail}")
        for agent, globs in rt.unenforced(pipeline, report).items():  # DF-64
            out.warn(
                f"{agent}: deny globs checked by the file tools only, not the OS sandbox: "
                + ", ".join(globs)
            )
        engine = rt.engine(run_budget_usd=limit, regime=rt.pipeline(pipeline).regime)
        run_id = run_id or new_run_id(pipeline)
        state = drive(
            rt.redact(gateway),
            rt.store.path(run_id).parent,
            lambda: engine.start(pipeline, given, rt.user, run_id),
        )
        _finish(out, state)

    @app.command("resume")
    def resume(
        ctx: typer.Context,
        run_id: str,
        extra_minutes: Annotated[int, typer.Option(min=0, help="More run time.")] = 0,
        extra_budget: Annotated[str | None, typer.Option(help="More run budget, USD.")] = None,
        allow: Annotated[list[str] | None, typer.Option(help="Approve a paused tool call.")] = None,
        deny: Annotated[list[str] | None, typer.Option(help="Refuse a paused tool call.")] = None,
        skip: Annotated[
            bool, typer.Option("--skip", help="Skip the step a condition pause is about.")
        ] = False,
    ) -> None:
        """Continue a paused or interrupted run."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        state = _load(rt, run_id)
        decisions: dict[str, bool | str] | None = None
        if allow or deny:
            decisions = {call: True for call in allow or []} | {call: False for call in deny or []}
        engine = rt.engine(regime=state.regime)
        extra = _usd(extra_budget, "--extra-budget") or Decimal(0)
        state = drive(
            rt.redact(rt.gateway()),
            rt.store.path(run_id).parent,
            lambda: engine.resume(
                run_id,
                extra_minutes=extra_minutes,
                extra_budget_usd=extra,
                decisions=decisions,
                skip=skip,
            ),
        )
        _finish(out, state)

    @app.command("status")
    def status(ctx: typer.Context, run_id: Annotated[str | None, typer.Argument()] = None) -> None:
        """One run in detail, or the most recent runs."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        if run_id is None:
            _list(out, rt, show_all=True, limit=10)
            return
        state = _load(rt, run_id)
        out.result(summary(state), show_state(state))

    @app.command("runs")
    def runs(
        ctx: typer.Context,
        show_all: Annotated[bool, typer.Option("--all", help="Finished runs too.")] = False,
    ) -> None:
        """This repository's runs that are not finished (all of them with --all)."""
        _list(ctx.obj, Runtime(Workspace.load()), show_all=show_all)

    @app.command("stop")
    def stop(
        ctx: typer.Context,
        run_id: str,
        reason: Annotated[str, typer.Option(help="Why the run is stopped (recorded).")],
    ) -> None:
        """End a run that is not finished; its trees are kept for inspection."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        state = _load(rt, run_id)
        engine = rt.engine(regime=state.regime, offline=True)
        with engine_errors():
            state = asyncio.run(engine.stop(run_id, rt.user, reason))
        out.result(summary(state), show_state(state))

    @app.command("logs")
    def logs(
        ctx: typer.Context,
        run_id: str,
        step: Annotated[str | None, typer.Option(help="A step id, or <step>-<iteration>.")] = None,
        level: Annotated[str | None, typer.Option(help="Lowest level shown.")] = None,
    ) -> None:
        """A run's per-step logs (secrets redacted when written)."""
        out: Output = ctx.obj
        if level is not None and level.lower() not in LEVELS:
            raise CliError(f"--level is one of {', '.join(LEVELS)}", ExitCode.USAGE)
        rt = Runtime(Workspace.load())
        _load(rt, run_id)
        records = read_logs(rt.store.path(run_id).parent, step, level)
        out.result(records, lambda c: c.print(render_logs(records), markup=False, end=""))


# ── helpers ──────────────────────────────────────────────────────────────────


def _inputs(pairs: list[str]) -> dict[str, str]:
    given = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise CliError(f"--input {pair!r} is not key=value", ExitCode.USAGE)
        given[key.strip()] = value
    return given


def _usd(text: str | None, flag: str) -> Decimal | None:
    if text is None:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        amount = Decimal(-1)
    if not amount.is_finite() or amount < 0:
        raise CliError(f"{flag} is an amount in USD, not {text!r}", ExitCode.USAGE)
    return amount


def _load(rt: Runtime, run_id: str) -> RunState:
    with engine_errors():
        return rt.store.load(run_id)


def _finish(out: Output, state: RunState) -> None:
    out.result(summary(state), show_state(state))
    raise typer.Exit(exit_code_for(state))


def _all_runs(rt: Runtime) -> list[RunState]:
    found = []
    root = rt.store.runs_root
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        if (folder / "run.json").is_file():
            try:
                found.append(rt.store.load(folder.name))
            except RunStateError:
                continue  # unreadable: `agentic status <run>` says why
    return sorted(found, key=lambda s: s.updated_at, reverse=True)


def _list(out: Output, rt: Runtime, *, show_all: bool, limit: int | None = None) -> None:
    states = [
        s for s in _all_runs(rt) if show_all or s.status not in ("completed", "failed", "stopped")
    ][:limit]
    rows = [
        {
            "run_id": s.run_id,
            "pipeline": s.pipeline,
            "status": s.status,
            "waiting_on": s.pending.id if s.pending else None,
            "updated_at": s.updated_at.isoformat(),
            "cost_usd": str(spent(s)),
        }
        for s in states
    ]

    def show(console) -> None:
        if not rows:
            console.print("no runs", markup=False)
            return
        table = output_table("run", "pipeline", "status", "waiting on", "updated", "cost USD")
        for r in rows:
            table.add_row(
                r["run_id"],
                r["pipeline"],
                r["status"],
                r["waiting_on"] or "",
                r["updated_at"][:19],
                r["cost_usd"],
            )
        console.print(table)

    out.result(rows, show)


def _show_plan(plan: dict):
    def agent_line(step: dict, indent: str) -> str:
        if not step.get("resolved"):
            return f"{indent}{step['id']}: agent {step['agent']} (does not resolve, see below)"
        limits = step["limits"]
        line = (
            f"{indent}{step['id']}: {step['agent']}@{step['version']} ({step['type']}, "
            f"{step['layer']}) - tier {step['tier']} -> {step['model']} on {step['backend']}; "
            f"budget ${limits['budget_usd']:g}, {limits['max_turns']} turns, "
            f"{limits['max_minutes']} min, {limits['children']} children"
        )
        if step.get("when"):
            line += f"; when {step['when']}"
        if step.get("unenforced_deny_paths"):
            line += (
                "; deny globs checked by the file tools only (not the OS sandbox): "
                + ", ".join(step["unenforced_deny_paths"])
            )
        return line

    def show(console) -> None:
        console.print(
            f"plan for {plan['pipeline']} {plan['version']} ({plan['layer']}): "
            f"regime {plan['regime']}, sandbox {plan['sandbox']}, policy {plan['policy_version']}",
            markup=False,
        )
        for step in plan["steps"]:
            if step["kind"] == "agent":
                console.print(agent_line(step, "  "), markup=False)
            elif step["kind"] == "gate":
                console.print(
                    f"  gate {step['id']}: approved by {step['who_approves']}", markup=False
                )
            elif step["kind"] == "loop":
                console.print(
                    f"  loop {step['id']}: up to {step['max_iterations']} times until "
                    f"{step['until']} (then {step['on_exhausted']})",
                    markup=False,
                )
                for inner in step["steps"]:
                    if inner["kind"] == "agent":
                        console.print(agent_line(inner, "    "), markup=False)
                    else:
                        console.print(f"    {inner['id']}: action {inner['action']}", markup=False)
            else:
                console.print(f"  {step['id']}: action {step['action']}", markup=False)
        preflight = plan["preflight"]
        console.print(
            "preflight passed" if preflight["passed"] else "preflight failed", markup=False
        )
        for p in preflight["problems"]:
            kind = " (warning)" if p["severity"] == "warning" else ""
            policy = f" (policy: {p['policy']})" if p["policy"] is not None else ""
            console.print(
                f"  {p['step'] or 'pipeline'}: {p['check']}{kind}: {p['detail']}{policy}",
                markup=False,
            )
        console.print("nothing was spent (dry run)", markup=False)

    return show
