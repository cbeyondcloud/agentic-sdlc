"""The learning loop, L1-L3 (M3-11..M3-13; spec §24):

    agentic learn feedback [--run R] [--org]          the corrections recorded so far
    agentic learn capture-pr <run>                    read the run's pull request reviews
    agentic learn analyse [<run>]                     the Learning Analyst on new records
    agentic learn attribute <feedback-id> --agent A [--agent B] --reason TEXT
    agentic learn proposals [--org]                   proposals made from repeated corrections
    agentic learn sync [<run>]                        send queued feedback to the control plane

Gate rejections and edits are recorded by `agentic reject` / `agentic edit` themselves.
A review comment, a "changes requested" review, and a change asked for on lines the Code
Review agent passed are read from the run's pull request by `capture-pr`. Each record is
attributed by the rules, and by the Learning Analyst where they do not decide; a person can
correct that with `attribute` (in org mode at the control plane, audited). Three similar
corrections of one agent within 30 days (org policy may tighten that) make a proposal.
A proposal changes nothing by itself: approving and applying it come later (L4-L6).
"""

from __future__ import annotations

from typing import Annotated

import typer

from agentic.hosting.base import FeedbackHost
from agentic.learning.capture import pr_feedback, review_misses
from agentic.learning.store import LocalLearningStore
from agentic_cli import learning
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import connect
from agentic_cli.output import Output, table
from agentic_cli.publishing import REMOTE, published
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons.ledger import run_ids

RunOption = Annotated[str | None, typer.Option("--run", help="Only this run's records.")]
OrgOption = Annotated[
    bool, typer.Option("--org", help="Read the team's records from the control plane.")
]


def register(app: typer.Typer) -> None:
    group = typer.Typer(
        help="The learning loop: human corrections, who learns, and proposals.",
        no_args_is_help=True,
    )
    app.add_typer(group, name="learn")

    @group.command("feedback")
    def feedback(ctx: typer.Context, run: RunOption = None, org: OrgOption = False) -> None:
        """The feedback records: each correction, who learns from it, and its lesson."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        if org:
            with _client(rt) as client:
                rows = [
                    item.model_dump(mode="json") for item in client.feedback_records(run_id=run)
                ]
        else:
            found = learning.states(rt, run) if run else learning.all_states(rt)
            rows = [_row(fid, state) for fid, state in found.items()]
        out.result(rows, lambda console: _show_feedback(console, rows))

    @group.command("capture-pr")
    def capture_pr(ctx: typer.Context, run_id: str) -> None:
        """Record the run's pull request review comments, changes requested and review
        misses as feedback (agent-made pull requests opened by `agentic pr open`)."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        capture = learning.FeedbackCapture(rt, warn=out.warn)
        recorded = capture.capture(run_id, _pr_records(rt, run_id))
        data = {"run_id": run_id, "recorded": [_record(r) for r in recorded]}
        out.result(
            data,
            lambda c: c.print(
                f"{len(recorded)} new feedback record(s) from run {run_id}'s pull request",
                markup=False,
            ),
        )

    @group.command("analyse")
    def analyse_cmd(
        ctx: typer.Context,
        run_id: Annotated[str | None, typer.Argument(help="One run (default: every run).")] = None,
    ) -> None:
        """Run the Learning Analyst on records it has not seen: a one-line lesson for each,
        and who learns where the rules did not decide (one fast-tier call per record)."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        capture = learning.FeedbackCapture(rt, warn=out.warn)
        done: list[str] = []
        for run in [run_id] if run_id else run_ids(rt.ws.repo):
            done += learning.analyse_run(rt, run, capture)
        out.result({"analysed": done}, lambda c: c.print(f"analysed {len(done)} record(s)"))

    @group.command("attribute")
    def attribute(
        ctx: typer.Context,
        feedback_id: Annotated[str, typer.Argument(metavar="FEEDBACK-ID")],
        agent: Annotated[
            list[str], typer.Option("--agent", help="An agent that should learn (repeat).")
        ],
        reason: Annotated[str, typer.Option("--reason", help="Why: recorded in the audit log.")],
    ) -> None:
        """Correct which agents learn from a record (in org mode at the control plane,
        audited; the record's author or a holder of agent.approve.repo in its team)."""
        out: Output = ctx.obj
        if not reason.strip():
            raise CliError("--reason must say why", ExitCode.USAGE)
        rt = Runtime(Workspace.load())
        agents = list(dict.fromkeys(a.strip() for a in agent if a.strip()))
        if rt.ws.settings.get("org"):
            with _client(rt) as client:
                info = client.correct_attribution(feedback_id, agents, reason.strip())
            data = info.model_dump(mode="json")
        else:
            capture = learning.FeedbackCapture(rt, warn=out.warn)
            run = capture.correct(feedback_id, agents, reason.strip(), rt.known_user())
            data = {"id": feedback_id, "run_id": run, "agents": agents}
        out.result(data, lambda c: c.print(f"{feedback_id}: {', '.join(agents)} learn from it"))

    @group.command("proposals")
    def proposals(ctx: typer.Context, org: OrgOption = False) -> None:
        """The proposals made from repeated corrections. Each changes nothing until a
        person approves it (later: `agentic learn approve`)."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        if org or rt.ws.settings.get("org"):
            with _client(rt) as client:
                rows = [p.model_dump(mode="json") for p in client.proposals()]
        else:
            learning.refresh_proposals(rt)
            store = LocalLearningStore(rt.ws.repo / learning.LEARNING)
            rows = [_proposal(p) for p in store.proposals()]
        out.result(rows, lambda console: _show_proposals(console, rows))

    @group.command("sync")
    def sync(
        ctx: typer.Context,
        run_id: Annotated[str | None, typer.Argument(help="One run (default: every run).")] = None,
    ) -> None:
        """Send the feedback the control plane has not had yet."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        sent = {
            r: learning.deliver(rt, r, out.warn)
            for r in ([run_id] if run_id else run_ids(rt.ws.repo))
        }
        out.result(
            {"sent": sent}, lambda c: c.print(f"sent {sum(sent.values())} feedback entr(ies)")
        )


def _client(rt: Runtime):
    client = rt.control_plane()
    if client is None:
        raise CliError(
            "this needs org mode with a control plane",
            ExitCode.REFUSED,
            hint="run `agentic login --for control-plane`",
        )
    return client


def _pr_records(rt: Runtime, run_id: str) -> list:
    """The run's pull request feedback and review misses, read through the host adapter."""
    record = published(rt.store.runs_root, run_id)
    agents, tdd_skipped, findings, reviewer = learning.run_agents(rt, run_id)
    bots = [str(login) for login in rt.ws.settings.get("learning_bot_logins") or []]
    found = []
    for entry in record.get("pull_requests") or []:
        with connect(rt.ws.repo, rt.ws.settings, remote=REMOTE) as conn, conn.errors():
            host = conn.provider
            if not isinstance(host, FeedbackHost):
                raise CliError(
                    f"the {conn.kind} adapter cannot read pull request reviews yet",
                    ExitCode.REFUSED,
                )
            number = int(entry["number"])
            records = pr_feedback(
                run_id=run_id,
                repo=f"{conn.repo.host}/{conn.repo.slug}",
                number=number,
                reviews=host.pull_request_reviews(conn.repo, number),
                comments=host.pull_request_review_comments(conn.repo, number),
                agent_logins=bots,
                at=learning._now(),
                tdd_skipped=tdd_skipped,
            )
            found += records + review_misses(records, findings, reviewer=reviewer)
    return found


def _record(record) -> dict:
    return {"id": record.id, "signal": record.signal, "actor": record.actor, "text": record.text}


def _row(fid: str, state) -> dict:
    attribution = state.attribution
    return {
        "id": fid,
        "run_id": state.record.run_id,
        "signal": state.record.signal,
        "actor": state.record.actor,
        "text": state.record.text,
        "agents": attribution.agents if attribution else [],
        "method": attribution.method if attribution else None,
        "lesson": state.lesson,
    }


def _proposal(proposal) -> dict:
    return {
        "id": proposal.id,
        "agent": proposal.agent,
        "scope": proposal.scope,
        "lesson": proposal.lesson,
        "evidence": list(proposal.evidence),
        "status": proposal.status,
        "created_at": proposal.created_at.isoformat(),
    }


def _agents(row: dict) -> str:
    if "agents" in row:
        return ", ".join(row["agents"]) or "(undecided)"
    learners = (row.get("attribution") or {}).get("learners") or []
    return ", ".join(x["agent"] for x in learners) or "(undecided)"


def _show_feedback(console, rows: list[dict]) -> None:
    if not rows:
        console.print("no feedback recorded yet")
        return
    shown = table("ID", "SIGNAL", "WHO LEARNS", "LESSON")
    for row in rows:
        shown.add_row(row["id"], row["signal"], _agents(row), row.get("lesson") or "")
    console.print(shown)


def _show_proposals(console, rows: list[dict]) -> None:
    if not rows:
        console.print("no proposals yet (a lesson needs repeated similar corrections)")
        return
    shown = table("ID", "AGENT", "SCOPE", "STATUS", "EVIDENCE", "LESSON")
    for row in rows:
        shown.add_row(
            row["id"],
            row["agent"],
            row["scope"],
            row["status"],
            str(len(row["evidence"])),
            row["lesson"],
        )
    console.print(shown)
