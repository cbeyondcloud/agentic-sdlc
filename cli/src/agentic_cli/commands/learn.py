"""The learning loop, L1-L3 (M3-11..M3-13; spec §24):

    agentic learn feedback [--run R] [--org]          the corrections recorded so far
    agentic learn capture-pr <run>                    read the run's pull request reviews
    agentic learn analyse [<run>]                     the Learning Analyst on new records
    agentic learn attribute <feedback-id> --agent A [--agent B] --reason TEXT
    agentic learn proposals [--org]                   proposals made from repeated corrections
    agentic learn sync [<run>]                        send queued feedback to the control plane

and L4-L6 (M3-14..M3-16):

    agentic learn list [--status S]                   proposals, with who would decide them
    agentic learn show <id>                           the change: diff, evidence, eval case
    agentic learn approve <id>                        the owner approves; the change is applied
    agentic learn edit <id> [--kind K] [--lesson T]   retarget or reword it before deciding
    agentic learn reject <id> [--reason T]            remembered: not proposed again as is
    agentic learn defer <id>                          decide later (stays in the list)
    agentic learn endorse <id>                        back it (weight, never a decision)
    agentic learn propose --from <feedback-id>        teach one correction now
    agentic learn revert <id>                         undo an applied learning

Gate rejections and edits are recorded by `agentic reject` / `agentic edit` themselves.
A review comment, a "changes requested" review, and a change asked for on lines the Code
Review agent passed are read from the run's pull request by `capture-pr`. Each record is
attributed by the rules, and by the Learning Analyst where they do not decide; a person can
correct that with `attribute` (in org mode at the control plane, audited). Three similar
corrections of one agent within 30 days (org policy may tighten that) make a proposal.
A proposal changes nothing by itself: only its owner's `approve` applies it (D29, D30) -
a personal skill or note in `~/.agentic`, or a pull request for a shared one.
"""

from __future__ import annotations

import re
from typing import Annotated

import typer

from agentic.hosting import RepoRef
from agentic.hosting.base import FeedbackHost
from agentic.learning.capture import pr_feedback, review_misses
from agentic.learning.changes import CHANGE_KINDS
from agentic.learning.records import AGENT_NAME
from agentic_cli import learning, learning_apply
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import connect, connect_to
from agentic_cli.output import Output, table
from agentic_cli.publishing import REMOTE, published
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons.ledger import run_id_refusal, run_ids

RunOption = Annotated[str | None, typer.Option("--run", help="Only this run's records.")]
OrgOption = Annotated[
    bool, typer.Option("--org", help="Read the team's records from the control plane.")
]
IdArgument = Annotated[str, typer.Argument(metavar="PROPOSAL-ID")]
_AGENT = re.compile(AGENT_NAME)


def _run_id(value: str | None) -> str | None:
    """DF-730: a bad run id is a usage error with a hint, never a traceback."""
    if value is not None and (why := run_id_refusal(value)):
        raise CliError(why, ExitCode.USAGE, hint="`agentic status` lists this repository's runs")
    return value


def _agent_names(names: list[str]) -> list[str]:
    agents = list(dict.fromkeys(a.strip() for a in names if a.strip()))
    bad = [a for a in agents if not _AGENT.fullmatch(a)] or ([] if agents else [""])
    if bad:
        raise CliError(
            f"{bad[0]!r} is not an agent name",
            ExitCode.USAGE,
            hint="an agent name is lowercase letters, digits and hyphens, as its file names it "
            "(developer, code-review)",
        )
    return agents


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
        run = _run_id(run)
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
        _run_id(run_id)
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
        _run_id(run_id)
        rt = Runtime(Workspace.load())
        capture = learning.FeedbackCapture(rt, warn=out.warn)
        done: list[str] = []
        failed: list[str] = []
        for run in [run_id] if run_id else run_ids(rt.ws.repo):
            analysed, missed = learning.analyse_run(rt, run, capture)
            done, failed = done + analysed, failed + missed
        out.result(
            {"analysed": done, "failed": failed},
            lambda c: c.print(f"analysed {len(done)} record(s), {len(failed)} without a verdict"),
        )
        if failed:  # kept unanalysed: the next `learn analyse` tries them again
            out.warn(
                f"the Learning Analyst gave {len(failed)} record(s) no verdict; run "
                "`agentic learn analyse` again later, or `agentic learn attribute` them"
            )
            raise typer.Exit(ExitCode.ERROR)

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
        agents = _agent_names(agent)
        rt = Runtime(Workspace.load())
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
            rows = [_proposal(p) for p in learning_apply.proposals(rt)]
        out.result(rows, lambda console: _show_proposals(console, rows))

    register_decisions(group)

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


def register_decisions(group: typer.Typer) -> None:
    """L4-L6: show a proposal as a change, decide it (its owner only), apply, revert."""

    @group.command("list")
    def list_cmd(
        ctx: typer.Context,
        status: Annotated[
            str | None, typer.Option("--status", help="Only this status (e.g. pending).")
        ] = None,
    ) -> None:
        """Every proposal, its status and who would decide it (target scope)."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        if rt.ws.settings.get("org"):
            rows = learning_apply.org_list(rt, status)
        else:
            rows = []
            for p in learning_apply.proposals(rt):
                evidence = learning_apply.evidence_of(rt, p)
                target = learning_apply.target_scope(rt, p, evidence)
                rows.append({**_proposal(p), "target": target, "evidence": len(p.evidence)})
            rows = [r for r in rows if status is None or r["status"] == status]
        out.result(rows, lambda console: _show_list(console, rows))

    @group.command("show")
    def show(ctx: typer.Context, proposal_id: IdArgument) -> None:
        """The change a proposal would make: target, diff, the corrections behind it and
        its regression eval case. Nothing changes until its owner approves it."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        data = learning_apply.show(rt, proposal_id)
        out.result(data, lambda console: _show_draft(console, data))

    @group.command("approve")
    def approve(ctx: typer.Context, proposal_id: IdArgument) -> None:
        """Approve a proposal you own and apply it: a personal change is written into your
        ~/.agentic; a shared one is a pull request on a new branch."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        data = learning_apply.approve_any(rt, proposal_id, out.warn)
        line = _applied_line(proposal_id, data["applied"])
        out.result(data, lambda c: c.print(line, markup=False))

    @group.command("edit")
    def edit(
        ctx: typer.Context,
        proposal_id: IdArgument,
        kind: Annotated[
            str | None,
            typer.Option("--kind", help=f"Retarget it: {', '.join(CHANGE_KINDS)}."),
        ] = None,
        lesson: Annotated[str | None, typer.Option("--lesson", help="Reword the lesson.")] = None,
    ) -> None:
        """Change what a proposal would do before deciding it (its owner only)."""
        out: Output = ctx.obj
        if not kind and not (lesson or "").strip():
            raise CliError("say what to change: --kind and/or --lesson", ExitCode.USAGE)
        if kind and kind not in CHANGE_KINDS:
            raise CliError(
                f"{kind!r} is not a change kind",
                ExitCode.INVALID,
                hint=f"one of {', '.join(CHANGE_KINDS)}",
            )
        rt = Runtime(Workspace.load())
        data = learning_apply.edit_any(rt, proposal_id, kind, lesson, out.warn)
        out.result(data, lambda c: c.print(f"{proposal_id}: {data['kind']} {data['path']}"))

    def decision(action: str, done: str):
        def command(
            ctx: typer.Context,
            proposal_id: IdArgument,
            reason: Annotated[
                str, typer.Option("--reason", help="Why (recorded with the decision).")
            ] = "",
        ) -> None:
            out: Output = ctx.obj
            rt = Runtime(Workspace.load())
            row = learning_apply.decide_any(rt, proposal_id, action, reason.strip(), out.warn)
            out.result(row, lambda c: c.print(f"{proposal_id}: {done}"))

        return command

    group.command("reject", help="Say no: the lesson is not proposed again without new evidence.")(
        decision("reject", "rejected")
    )
    group.command("defer", help="Decide later: it stays in the list as deferred.")(
        decision("defer", "deferred")
    )

    @group.command("endorse")
    def endorse(ctx: typer.Context, proposal_id: IdArgument) -> None:
        """Back a proposal: it adds weight for its owner, and never approves it."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        row = learning_apply.endorse_any(rt, proposal_id)
        out.result(row, lambda c: c.print(f"{proposal_id}: endorsed"))

    @group.command("propose")
    def propose_cmd(
        ctx: typer.Context,
        feedback_id: Annotated[
            str, typer.Option("--from", metavar="FEEDBACK-ID", help="The correction to teach.")
        ],
        agent: Annotated[
            str | None, typer.Option("--agent", help="Which of its learners (default: first).")
        ] = None,
    ) -> None:
        """Teach one correction now, without waiting for it to repeat (an explicit request)."""
        out: Output = ctx.obj
        if agent is not None:
            agent = _agent_names([agent])[0]
        rt = Runtime(Workspace.load())
        row = learning_apply.propose_any(rt, feedback_id, agent)
        out.result(row, lambda c: c.print(f"proposed {row['id']}: {row['lesson']}"))

    @group.command("revert")
    def revert(ctx: typer.Context, proposal_id: IdArgument) -> None:
        """Undo an applied learning: your ~/.agentic file as it was, or a revert pull
        request for a shared one."""
        out: Output = ctx.obj
        rt = Runtime(Workspace.load())
        data = learning_apply.revert_any(rt, proposal_id, out.warn)
        kind = data["reverted"]["kind"]
        out.result(data, lambda c: c.print(f"{proposal_id}: reverted ({kind})"))


def _applied_line(pid: str, applied: dict) -> str:
    if applied.get("kind") == "pull-request":
        return f"{pid}: approved; pull request #{applied['number']} {applied['url']}"
    return f"{pid}: approved and applied to " + ", ".join(applied.get("paths", []))


def _show_list(console, rows: list[dict]) -> None:
    if not rows:
        console.print("no proposals yet (a lesson needs repeated similar corrections)")
        return
    shown = table("ID", "AGENT", "TARGET", "STATUS", "EVIDENCE", "LESSON")
    for row in rows:
        shown.add_row(
            row["id"],
            row["agent"],
            str(row.get("target", "")),
            row["status"],
            str(row["evidence"]),
            row["lesson"],
        )
    console.print(shown)


def _show_draft(console, data: dict) -> None:
    made = data["draft"]
    console.print(
        f"{data['id']} ({data['status']}): {made['kind']} -> {made['path']} "
        f"[{made['scope']}, decided by {made['owner']}]",
        markup=False,
    )
    console.print(f"lesson: {made['lesson']}", markup=False)
    for item in made["evidence"]:
        console.print(
            f"  evidence {item['feedback_id']} by {item['actor']}: {item['text']}", markup=False
        )
    console.print(made["diff"], markup=False, highlight=False)
    console.print(f"regression case: {made['eval_path']}", markup=False)
    for problem in made["problems"]:
        console.print(f"not instruction-only: {problem}", markup=False)


def _client(rt: Runtime):
    client = rt.control_plane()
    if client is None:
        raise CliError(
            "this needs org mode with a control plane",
            ExitCode.REFUSED,
            hint="run `agentic login --for control-plane`",
        )
    return client


def _connection(rt: Runtime, entry: dict):
    """The host connection for one recorded pull request: the repository `pr.json` names
    for it (a component's pull request may live in another repository than the
    workspace's), or - for a record without one - the workspace's own remote."""
    host, slug = str(entry.get("host") or ""), str(entry.get("repo") or "")
    if host and "/" in slug:
        owner, name = slug.rsplit("/", 1)
        return connect_to(RepoRef(host, owner, name), rt.ws.settings, remote=REMOTE)
    return connect(rt.ws.repo, rt.ws.settings, remote=REMOTE)


def _pr_records(rt: Runtime, run_id: str) -> list:
    """The run's pull request feedback and review misses, read through the host adapter.
    The pull request's author is never a reviewer of it; `learning_bot_logins` (settings)
    names further accounts whose comments are not corrections."""
    record = published(rt.store.runs_root, run_id)
    agents, tdd_skipped, findings, reviewer = learning.run_agents(rt, run_id)
    bots = [str(login) for login in rt.ws.settings.get("learning_bot_logins") or []]
    found = []
    for entry in record.get("pull_requests") or []:
        with _connection(rt, entry) as conn, conn.errors():
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
                pr_author=host.get_pull_request(conn.repo, number).author,
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
