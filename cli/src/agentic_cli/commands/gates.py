"""Deciding at a gate (M1-55; spec §6.1, §8.3, §24 step 8):

    agentic show <run>                              the handoffs the gate pins, and the diff
    agentic approve <run> [--gate G] [--notes T]
    agentic reject <run> --feedback T [--gate G]    the work goes back with the feedback
    agentic edit <run> [--gate G] [--file PATH]     edit a pinned handoff in your editor; it
                                                    is validated, then approved
    agentic waiting                                 every gate waiting in this repository

The gate defaults to the one the run waits at; naming another is refused. Each decision
is one entry in the run's approval ledger and a `gate_decided` event (Zyroflo), taken as
the workspace's person. Standalone mode has no RBAC: every role is held (M2-03 brings
org roles); four-eyes still applies where the gate or the regime asks for it.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.markdown import Markdown
from rich.syntax import Syntax

from agentic.engine.gates import Approve, Decision, Edit, Reject, StandaloneAuthorizer
from agentic.engine.state import RunState
from agentic.yamlio import YamlFileError, split_front_matter
from agentic_cli.commands.runs import _all_runs, _load
from agentic_cli.driving import drive, exit_code_for, show_state, summary
from agentic_cli.editor import edit_file
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output, printable
from agentic_cli.output import table as output_table
from agentic_cli.runtime import STATE, Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons.gates import self_approval_refusal
from sdlc_commons.worktrees import RunWorktrees, WorktreeError, worktree_git

Gate = Annotated[str | None, typer.Option("--gate", help="The gate (default: the waiting one).")]
_EDITED = re.compile(r"(\.edited-\d+\.md)+$")


def register(app: typer.Typer) -> None:
    @app.command("approve")
    def approve(
        ctx: typer.Context,
        run_id: str,
        gate: Gate = None,
        notes: Annotated[str, typer.Option(help="Recorded with the approval.")] = "",
    ) -> None:
        """Approve the handoffs the gate pins, as they are; the run continues."""
        rt, state = _waiting(run_id)
        _decide(ctx.obj, rt, state, Approve(gate or state.pending.id, rt.user, notes))

    @app.command("reject")
    def reject(
        ctx: typer.Context,
        run_id: str,
        feedback: Annotated[str, typer.Option(help="What to change (the agent reads it).")],
        gate: Gate = None,
    ) -> None:
        """Send the work back to the gate's step, with your feedback."""
        if not feedback.strip():
            raise CliError("--feedback must say what to change", ExitCode.USAGE)
        rt, state = _waiting(run_id)
        _decide(ctx.obj, rt, state, Reject(gate or state.pending.id, rt.user, feedback))

    @app.command("edit")
    def edit(
        ctx: typer.Context,
        run_id: str,
        gate: Gate = None,
        file: Annotated[
            str | None, typer.Option("--file", help="Which pinned handoff (when several).")
        ] = None,
        notes: Annotated[str, typer.Option(help="Recorded with the approval.")] = "",
    ) -> None:
        """Edit a pinned handoff in $VISUAL / $EDITOR (Notepad on Windows); the edit is
        validated as a handoff, then approved."""
        rt, state = _waiting(run_id)
        pinned = sorted(state.pending.artefacts)
        if file is None and len(pinned) != 1:
            raise CliError(
                f"the gate pins {len(pinned)} files: name one with --file",
                ExitCode.USAGE,
                hint=", ".join(pinned),
            )
        rel = file or pinned[0]
        if rel not in pinned:
            raise CliError(
                f"{rel} is not pinned by this gate", ExitCode.USAGE, hint=", ".join(pinned)
            )
        original = (rt.store.path(run_id).parent / rel).read_text(encoding="utf-8")
        draft = rt.ws.repo / STATE / run_id / "edit" / Path(rel).name
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(original.encode("utf-8"))
        edit_file(draft)
        text = draft.read_text(encoding="utf-8").replace("\r\n", "\n")
        if text == original.replace("\r\n", "\n"):
            raise CliError(
                "nothing changed in the editor; nothing was decided",
                ExitCode.REFUSED,
                hint=f"to accept it as it is: agentic approve {run_id}",
            )
        decision = Edit(gate or state.pending.id, rt.user, rel, text, notes)
        _decide(ctx.obj, rt, state, decision)
        draft.unlink(missing_ok=True)

    @app.command("show")
    def show(ctx: typer.Context, run_id: str) -> None:
        """The handoffs the run waits on, and the code changes since the last gate."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        state = _load(rt, run_id)
        shown = _shown(rt, state)
        out.result(shown, _show(shown))

    @app.command("waiting")
    def waiting(ctx: typer.Context) -> None:
        """Every gate (and tool approval) waiting for a person in this repository's runs."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        now = datetime.now(UTC)
        rows = []
        for state in sorted(_all_runs(rt), key=lambda s: s.pending.since if s.pending else now):
            pending = state.pending
            if pending is None or pending.kind not in ("gate", "approval"):
                continue
            rows.append(
                {
                    "run_id": state.run_id,
                    "pipeline": state.pipeline,
                    "gate": pending.id,
                    "approver_role": _role(rt, state, pending.id),
                    "step": pending.step,
                    "reason": pending.reason,
                    "since": pending.since.isoformat(),
                    "waiting_minutes": int((now - pending.since).total_seconds() // 60),
                    "starter": state.starter,
                }
            )

        def human(console) -> None:
            if not rows:
                console.print("nothing is waiting for you", markup=False)
                return
            table = output_table("run", "gate", "role", "waiting", "started by")
            for r in rows:
                table.add_row(
                    r["run_id"],
                    r["gate"],
                    r["approver_role"] or "",
                    _age(r["waiting_minutes"]),
                    r["starter"],
                )
            console.print(table)

        out.result(rows, human)


# ── helpers ──────────────────────────────────────────────────────────────────


def _waiting(run_id: str) -> tuple[Runtime, RunState]:
    rt = Runtime(Workspace.load())
    state = _load(rt, run_id)
    if state.pending is None or state.pending.kind != "gate":
        raise CliError(
            f"run {run_id} is not waiting at a gate (status: {state.status})",
            ExitCode.REFUSED,
            hint=f"agentic status {run_id}",
        )
    return rt, state


def _decide(out: Output, rt: Runtime, state: RunState, decision: Decision) -> None:
    engine = rt.engine(regime=state.regime)
    result = drive(
        rt.redact(rt.gateway()),
        rt.store.path(state.run_id).parent,
        lambda: engine.decide(
            state.run_id,
            decision,
            authorizer=StandaloneAuthorizer(),
            self_approval=self_approval_refusal,
        ),
    )
    redact = rt.redact()
    out.result(summary(result, redact), show_state(result, redact))
    raise typer.Exit(exit_code_for(result))


def _role(rt: Runtime, state: RunState, gate: str) -> str | None:
    try:
        pipeline = rt.ws.definitions().pipeline(state.pipeline)
    except Exception:  # the pipeline changed or is gone: the gate still shows
        return None
    for step in pipeline.steps:
        if getattr(step, "gate", None) == gate:
            return step.approver_role
    return None


def _age(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def _shown(rt: Runtime, state: RunState) -> dict:
    pending = state.pending
    run_dir = rt.store.path(state.run_id).parent
    handoffs = []
    for rel in sorted(pending.artefacts) if pending else []:
        try:
            text = (run_dir / rel).read_text(encoding="utf-8")
        except OSError as error:
            text = f"(cannot be read: {error})"
        handoffs.append({"path": rel, "text": text})
    return {
        "run_id": state.run_id,
        "status": state.status,
        "gate": pending.id if pending else None,
        "reason": pending.reason if pending else None,
        "handoffs": handoffs,
        "diff": _diff(rt, state),
    }


def _diff(rt: Runtime, state: RunState) -> str:
    """The code the gated steps changed: from the first gated step's base to the last
    one's checkpoint, per component tree."""
    pending = state.pending
    if pending is None:
        return ""
    gated = {_EDITED.sub("", rel) for rel in pending.artefacts}
    records = [r for r in state.history if r.handoff in gated and r.base and r.commit]
    if not records:
        return ""
    base, commit = records[0].base, records[-1].commit
    try:
        trees = RunWorktrees(rt.ws.repo, state.run_id).trees()
    except WorktreeError:
        return ""
    parts = []
    for name in sorted(commit):
        tree = trees.get(name)
        if tree is None or name not in base or base[name] == commit[name]:
            continue
        try:
            parts.append(worktree_git(tree.path, "diff", base[name], commit[name]))
        except WorktreeError as error:
            parts.append(f"# {name}: the diff cannot be shown ({error})")
    return "\n".join(part for part in parts if part)


def _show(shown: dict):
    def human(console) -> None:
        if shown["gate"] is None:
            console.print(f"run {shown['run_id']} is not waiting ({shown['status']})")
            return
        console.print(f"run {shown['run_id']} waits at {shown['gate']}: {shown['reason']}")
        for handoff in shown["handoffs"]:
            console.rule(handoff["path"])
            text = printable(handoff["text"])  # an agent's text cannot drive the terminal
            try:
                header, body = split_front_matter(text)
            except YamlFileError:  # not a handoff (an agent's free text): shown as it is
                header, body = "", text
            if header:
                console.print(Syntax(header, "yaml", word_wrap=True))
            console.print(Markdown(body))
        console.rule("code changes")
        if shown["diff"]:
            console.print(Syntax(printable(shown["diff"]), "diff", word_wrap=True))
        else:
            console.print("no code changes", markup=False)

    return human
