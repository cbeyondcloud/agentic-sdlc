"""Starting and following runs (M1-54; spec §8.2, §8.3, §24 steps 6-8):

    agentic run <pipeline> [--input k=v]... [--budget USD] [--run-id ID] [--dry-run]
    agentic resume <run> [--extra-minutes N] [--extra-budget USD] [--allow CALL]...
                         [--deny CALL]... [--skip]
    agentic status [<run>]
    agentic runs [--all]
    agentic stop <run> --reason TEXT
    agentic logs <run> [--step ID] [--level LEVEL] [--follow]   (M1-47's command)

`logs` shows the run's per-step logs, redacted once more on read. `--follow` keeps
printing new records until the run stops running (`--json --follow`: JSON Lines), then
exits with the run's exit code.

A run that waits for a person exits 4, one stopped (or paused at a limit) exits 5, a
dry run whose preflight fails exits 3 with the report.

`--extra-budget` raises the run's budget limit: it is refused (exit 2) for a run
started without `--budget`, and for a step that reached its agent's own budget.
`--skip` is a person's decision: it is recorded under the workspace's person (in org mode
with a control plane, the signed-in person, as for starting a run and approving a gate),
in the approval ledger, and in the regulated regime it may not be the person who started
the run (four-eyes).

A live run is refused before it is registered with the control plane for anything the
CLI can see itself (an existing run id, missing or unknown inputs): registering takes a
concurrency slot.
"""

import asyncio
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from rich import box

from agentic.engine.engine import TERMINAL, EngineError, check_inputs
from agentic.engine.state import RunState, RunStateError, RunStore
from agentic.telemetry.logs import LEVELS, LogTail, redact_record, render_logs
from agentic_cli.commands.catalog import show_definition_warnings
from agentic_cli.commands.killswitch import resume_scope
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
from agentic_cli.org_preflight import org_checks, plan_section
from agentic_cli.output import Output
from agentic_cli.output import table as output_table
from agentic_cli.runtime import Runtime, new_run_id
from agentic_cli.workspace import Workspace
from sdlc_commons.gates import self_approval_refusal


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
        if run_id is not None:
            _check_run_id(run_id)  # before the workspace, preflight and gateway (DF-219)
        rt = Runtime(Workspace.load())
        show_definition_warnings(out, rt.ws)
        run_id = run_id or new_run_id(pipeline)
        if dry_run:
            report = rt.preflight(pipeline, live=False)
            if report.passed:  # the org's answer for a run that would start (M2-21)
                answer = org_checks(rt, pipeline, report, run_id, dry_run=True)
            else:
                answer = None
            plan = rt.plan(pipeline, report)
            plan["org"] = plan_section(answer)
            out.result(plan, _show_plan(plan))
            raise typer.Exit(ExitCode.OK if report.passed else ExitCode.INVALID)
        limit = _usd(budget, "--budget")
        gateway = rt.gateway()  # before anything is installed or written
        report = rt.preflight(pipeline, live=True)
        engine = None
        if report.passed:
            # every local refusal comes first: registering takes one of the user's
            # concurrency slots, which a run that never starts would hold (review F3)
            _startable(rt, pipeline, given, run_id)
            engine = rt.engine(run_budget_usd=limit, regime=rt.pipeline(pipeline).regime)
            # registers the run (its concurrency slot) when the org agrees
            org_checks(rt, pipeline, report, run_id, dry_run=False)
        if not report.passed or engine is None:
            out.result({"preflight": report.text()}, lambda c: c.print(report.text(), markup=False))
            raise CliError("preflight failed: nothing was started", ExitCode.INVALID)
        starter = rt.user  # in org mode the signed-in person, as approvers are (review F1)
        for problem in report.problems:
            out.warn(f"{problem.step or 'pipeline'}: {problem.check}: {problem.detail}")
        for agent, globs in rt.unenforced(pipeline, report).items():  # DF-64
            out.warn(
                f"{agent}: deny globs checked by the file tools only, not the OS sandbox: "
                + ", ".join(globs)
            )
        # a live run prints nothing until it stops: name it now, for a Ctrl-C (DF-221); a
        # script gets one JSON line on stderr, stdout keeps its one result (DF-352)
        out.notice(
            {"started": run_id}, f"started run {run_id} (if interrupted: agentic resume {run_id})"
        )
        with sinks_reported(out, rt):
            state = drive(
                rt.redact(gateway),
                rt.store.path(run_id).parent,
                lambda: engine.start(pipeline, given, starter, run_id),
            )
        _finish(out, state, rt)

    @app.command("resume")
    def resume(
        ctx: typer.Context,
        run_id: Annotated[str | None, typer.Argument(help="The run to continue.")] = None,
        extra_minutes: Annotated[int, typer.Option(min=0, help="More run time.")] = 0,
        extra_budget: Annotated[str | None, typer.Option(help="More run budget, USD.")] = None,
        allow: Annotated[list[str] | None, typer.Option(help="Approve a paused tool call.")] = None,
        deny: Annotated[list[str] | None, typer.Option(help="Refuse a paused tool call.")] = None,
        skip: Annotated[
            bool, typer.Option("--skip", help="Skip the step a condition pause is about.")
        ] = False,
        scope: Annotated[
            str | None,
            typer.Option("--scope", help="Admins: clear the pause switches of this scope."),
        ] = None,
        team: Annotated[
            str | None, typer.Option("--team", help="With --scope: the team it was limited to.")
        ] = None,
    ) -> None:
        """Continue a paused or interrupted run (or, with --scope, end a pause switch)."""
        out: Output = ctx.obj
        granted = extra_minutes or extra_budget is not None or allow or deny or skip
        if (run_id is None) == (scope is None) or (scope is not None and granted):
            raise CliError(
                "resume takes a run, or --scope (and --team) to clear a pause switch",
                ExitCode.USAGE,
                hint="agentic resume <run>   or   agentic resume --scope team:<slug>",
            )
        if scope is not None:
            resume_scope(out, scope, team)
            return
        if team is not None:
            raise CliError("--team goes with --scope", ExitCode.USAGE)
        if skip and (extra_minutes or extra_budget is not None or allow or deny):
            raise CliError(
                "--skip goes on its own: it skips the step and grants nothing else",
                ExitCode.USAGE,
            )
        rt = Runtime(Workspace.load())
        state = _load(rt, run_id)
        decisions: dict[str, bool | str] | None = None
        if allow or deny:
            decisions = {call: True for call in allow or []} | {call: False for call in deny or []}
        engine = rt.engine(regime=state.regime)
        extra = _usd(extra_budget, "--extra-budget") or Decimal(0)
        who = rt.user if skip else None  # a person's decision: the signed-in one in org mode
        with sinks_reported(out, rt):
            state = drive(
                rt.redact(rt.gateway()),
                rt.store.path(run_id).parent,
                lambda: engine.resume(
                    run_id,
                    extra_minutes=extra_minutes,
                    extra_budget_usd=extra,
                    decisions=decisions,
                    skip=skip,
                    who=who,
                    self_approval=self_approval_refusal,
                ),
            )
        _finish(out, state, rt)

    @app.command("status")
    def status(ctx: typer.Context, run_id: Annotated[str | None, typer.Argument()] = None) -> None:
        """One run in detail, or the most recent runs."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        if run_id is None:
            _list(out, rt, show_all=True, limit=10)
            return
        state = _load(rt, run_id)
        out.result(summary(state, rt.redact()), show_state(state, rt.redact()))

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
        with sinks_reported(out, rt), engine_errors():  # run_closed reaches the sinks (F5)
            state = asyncio.run(engine.stop(run_id, rt.known_user(), reason))
        out.result(summary(state, rt.redact()), show_state(state, rt.redact()))

    @app.command("logs")
    def logs(
        ctx: typer.Context,
        run_id: str,
        step: Annotated[str | None, typer.Option(help="A step id, or <step>-<iteration>.")] = None,
        level: Annotated[str | None, typer.Option(help="Lowest level shown.")] = None,
        follow: Annotated[
            bool,
            typer.Option(
                "--follow",
                "-f",
                help="Keep printing new records until the run stops running; then exit "
                "with the run's exit code.",
            ),
        ] = False,
    ) -> None:
        """A run's per-step logs (secrets redacted when written, and again when shown)."""
        out: Output = ctx.obj
        if level is not None and level.lower() not in LEVELS:
            raise CliError(f"--level is one of {', '.join(LEVELS)}", ExitCode.USAGE)
        rt = Runtime(Workspace.load())
        running = _load(rt, run_id).status == "running"
        redact = _log_redactor(rt)
        tail = LogTail(rt.store.path(run_id).parent, step, level)
        if follow:
            _follow(out, rt, run_id, tail, redact)
            return
        # a running step's last line may be half written: show it once the run stopped
        records = [redact_record(r, redact) for r in tail.poll(final=not running)]
        out.result(records, lambda c: c.print(render_logs(records), markup=False, end=""))


# ── helpers ──────────────────────────────────────────────────────────────────

FOLLOW_INTERVAL_S = 0.5
_sleep = time.sleep  # replaced by the tests


def _log_redactor(rt: Runtime) -> Callable[[str], str]:
    """The redactor for shown logs: with the gateway key when one is configured, but
    reading logs never needs a gateway."""
    try:
        return rt.redact(rt.gateway())
    except CliError:
        return rt.redact()


def _follow(
    out: Output, rt: Runtime, run_id: str, tail: LogTail, redact: Callable[[str], str]
) -> None:
    """Print records as they are written, until the run is no longer running.

    The status is read BEFORE each poll: once a status read says the run stopped
    running, the poll after it has every record written before that, so the last
    records are never missed. A run whose process died stays `running`: Ctrl-C ends
    the follow (exit 130). It exits with the run's own exit code, as `run` would (0
    completed, 1 failed, 4 waiting for a person, 5 stopped; DF-285)."""
    while True:
        state = _load(rt, run_id)
        status = state.status
        for record in tail.poll(final=status != "running"):
            clean = redact_record(record, redact)
            out.stream(clean, render_logs([clean]))
        if status != "running":
            break
        _sleep(FOLLOW_INTERVAL_S)
    if not out.json_mode:
        out.err.print(f"run {run_id} is {status}", markup=False)
    raise typer.Exit(exit_code_for(state))


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


def _check_run_id(run_id: str) -> None:
    try:
        RunStore(Path(".")).path(run_id)  # the engine's own rule, without touching disk
    except RunStateError as error:
        raise CliError(f"--run-id: {error}", ExitCode.USAGE) from error


def _startable(rt: Runtime, pipeline: str, given: dict[str, str], run_id: str) -> None:
    """The engine's own start refusals, asked before the run is registered (review F3)."""
    with engine_errors():
        if rt.store.exists(run_id):
            raise EngineError(f"run {run_id} already exists - resume it instead")
        check_inputs(rt.pipeline(pipeline), given)


def _load(rt: Runtime, run_id: str) -> RunState:
    with engine_errors():
        return rt.store.load(run_id)


def _finish(out: Output, state: RunState, rt: Runtime) -> None:
    redact = rt.redact()  # the gateway key is known: the command asked for it
    out.result(summary(state, redact), show_state(state, redact))
    raise typer.Exit(exit_code_for(state))


@contextmanager
def sinks_reported(out: Output, rt: Runtime) -> Iterator[None]:
    """Around every use of an engine: however it ends - a result, a refusal, an error -
    the events it recorded get their bounded wait and report (review F5)."""
    try:
        yield
    finally:
        report_sinks(out, rt)


def report_sinks(out: Output, rt: Runtime) -> None:
    """After the engine stops: wait briefly for run events still on their way to the
    control plane, and say what stays queued (or why nothing is reported)."""
    if rt.sink_problem:
        out.warn(rt.sink_problem)
    for run_id, left in rt.close_sinks().items():
        out.warn(
            f"{left} event(s) of run {run_id} are queued for the control plane; "
            "`agentic sync` sends them"
        )


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
    states = [s for s in _all_runs(rt) if show_all or s.status not in TERMINAL][:limit]
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
    out.result(rows, _show_runs(rows))


WIDE_COLUMNS = 100  # below this the runs table drops pipeline and updated (DF-222 b)
PIPED_WIDTH = 160  # the table's width on a pipe or redirect, which has none (DF-351)
NARROW = ("run", "status", "waiting on", "cost USD")


def _show_runs(rows: list[dict]):
    def show(console) -> None:
        if not rows:
            console.print("no runs", markup=False)
            return
        # an 80-column terminal (macOS Terminal's default) cannot fit six columns beside
        # a whole run id; the pipeline is in the run id already. A pipe or redirect has no
        # width (Rich says 80): it gets every column, at a width that keeps them whole
        # (DF-351), unless COLUMNS says otherwise.
        piped = not console.is_terminal and "COLUMNS" not in os.environ
        width = max(console.width, PIPED_WIDTH) if piped else console.width
        wide = width >= WIDE_COLUMNS
        columns = ["run", "pipeline", "status", "waiting on", "updated", "cost USD"]
        table = output_table(*(columns if wide else [c for c in columns if c in NARROW]))
        if not wide:  # no borders, one space between columns: whole words in each
            table.box, table.show_edge, table.padding = box.SIMPLE_HEAD, False, (0, 1, 0, 0)
        for r in rows:
            cells = [r["run_id"], r["pipeline"], r["status"], r["waiting_on"] or ""]
            cells += [r["updated_at"][:19], r["cost_usd"]]
            table.add_row(*(cells if wide else [cells[0], cells[2], cells[3], cells[5]]))
        if not piped:
            console.print(table)
            return
        before = console.width  # Rich caps print(width=) at the console's own width
        console.width = width
        try:
            console.print(table)
        finally:
            console.width = before

    return show


def _show_plan(plan: dict):
    def layer(name: str) -> str:  # a standalone policy repo is not an org (DF-286)
        return "policy" if name == "org" and not plan.get("org") else name

    def agent_line(step: dict, indent: str) -> str:
        if not step.get("resolved"):
            return f"{indent}{step['id']}: agent {step['agent']} (does not resolve, see below)"
        limits = step["limits"]
        line = (
            f"{indent}{step['id']}: {step['agent']}@{step['version']} ({step['type']}, "
            f"{layer(step['layer'])}) - tier {step['tier']} -> {step['model']} on "
            f"{step['backend']}; "
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
            f"plan for {plan['pipeline']} {plan['version']} ({layer(plan['layer'])}): "
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
        if org := plan.get("org"):  # the control plane's checks (M2-21)
            for check in org["checks"]:
                mark = "ok" if check["passed"] else "REFUSED"
                console.print(f"  org {check['check']}: {mark} - {check['detail']}", markup=False)
            budget = org["team_budget_remaining_usd"]
            shown = f"${budget}" if budget is not None else "not measured"
            console.print(f"  team budget remaining: {shown}", markup=False)
        console.print("nothing was spent (dry run)", markup=False)

    return show
