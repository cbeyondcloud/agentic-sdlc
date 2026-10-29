"""The learning loop's decisions and changes in a workspace (M3-14..M3-16; spec §24 steps 4-6).

  * `draft_for` - a proposal as a concrete change (`agentic.learning.changes.draft`): the
    target file is read from `~/.agentic` (personal) or the working tree (repo), the
    evidence from the runs' feedback journals, a person's edits from the change record;
  * `decide` - approve, reject or defer, by the owner only (D30): a personal proposal by
    the person whose corrections it holds; a shared one, standalone, by whoever runs the
    command (there is no RBAC to ask: said so, and the repository's maintainers decide in
    the pull request review);
  * `apply` - only an approved proposal (D29): a personal change is written into
    `~/.agentic` through Zyroflo's learning log (`sdlc_commons.learning`), with its
    regression case; a repo change is a pull request on a new branch
    (`agentic/learning/<id>`) built on the remote default branch with git plumbing - the
    checkout, its index and its current branch never change, and nothing is ever
    committed to the default branch;
  * `revert` - Zyroflo's undo for a personal change; for a shared one a revert pull
    request that takes the learned block out of the default branch's file (the content
    before the learning). The regression case stays: it records the correction;
  * `pending_notice` - the end of a run says how many proposals wait for a decision.

Each step is recorded in the proposal's change record (`.agentic/local/learning/changes/`).
In org mode the control plane holds the proposals and decides who may act; the change is
still made from the approver's machine, and the control plane is told (`applied`,
`reverted`), so its audit chain shows every step.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentic.hosting import HostingError
from agentic.hosting.git import fetch_branch, push_branch, rev_parse
from agentic.learning.approval import DecisionError, may_decide, next_status
from agentic.learning.changes import Draft, DraftError, Evidence, draft, render_change, strip_change
from agentic.learning.clustering import Proposal, proposal_id
from agentic.learning.store import LocalLearningStore, StoreError
from agentic.yamlio import dump_yaml
from agentic_cli import learning, repo_setup
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import connect
from agentic_cli.publishing import REMOTE
from agentic_cli.skill_promotion import head_text
from sdlc_commons.learning import LearningApplyError, PersonalChanges

if TYPE_CHECKING:
    from agentic_cli.output import Output
    from agentic_cli.runtime import Runtime

_LOG = logging.getLogger("agentic.learning")
BRANCH = "agentic/learning/{id}"
BASE_REF = "refs/agentic/learning-base"
EVAL_PART = "#eval"
STORE_HINT = (
    "move .agentic/local/learning/proposals.json aside: the next `agentic learn list` "
    "rebuilds the proposals from the runs' feedback journals (decisions in it are lost)"
)
NO_RBAC = (
    "standalone: there is no RBAC to ask, so this shared decision proceeds; the "
    "repository's maintainers decide in the pull request review"
)


def _now() -> datetime:
    return datetime.now(UTC)


def store(rt: Runtime) -> LocalLearningStore:
    return LocalLearningStore(rt.ws.repo / learning.LEARNING)


def _store_errors(error: StoreError) -> CliError:
    return CliError(str(error), ExitCode.INVALID, hint=STORE_HINT)


def proposals(rt: Runtime) -> list[Proposal]:
    """The standalone proposals, refreshed from the runs' journals first (DF-736: a damaged
    file is named, with what to do about it)."""
    try:
        learning.refresh_proposals(rt)
        return store(rt).proposals()
    except StoreError as error:
        raise _store_errors(error) from None


def get(rt: Runtime, pid: str) -> Proposal:
    try:
        return store(rt).get(pid)
    except StoreError as error:
        if "no proposal" in str(error):
            raise CliError(
                str(error), ExitCode.INVALID, hint="`agentic learn list` lists them"
            ) from None
        raise _store_errors(error) from None


def record_of(rt: Runtime, pid: str) -> dict:
    try:
        return store(rt).change(pid) or {}
    except StoreError as error:
        raise _store_errors(error) from None


def evidence_of(rt: Runtime, proposal: Proposal) -> list[Evidence]:
    found = learning.all_states(rt)
    out = []
    for fid in proposal.evidence:
        state = found.get(fid)
        if state is None:
            continue
        record = state.record
        out.append(
            Evidence(
                feedback_id=fid,
                signal=record.signal,
                actor=record.actor,
                given_at=record.given_at,
                text=record.text,
                run_id=record.run_id,
                repo=record.repo,
            )
        )
    return out


def reader(rt: Runtime) -> Callable[[str, str], str | None]:
    """The current text of a target file: `~/.agentic` (home), the working tree (repo); a
    policy-repo file is read when the change is applied (None here: drafted as new)."""

    def read(root: str, path: str) -> str | None:
        base = {"home": rt.ws.home, "repo": rt.ws.repo}.get(root)
        if base is None:
            return None
        file = base.joinpath(*path.split("/"))
        try:
            return file.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as error:
            raise CliError(f"{file} cannot be read: {error}", ExitCode.ERROR) from None

    return read


def draft_for(
    rt: Runtime,
    proposal: Proposal,
    evidence: list[Evidence] | None = None,
    edit: dict | None = None,
) -> Draft:
    if edit is None:
        edit = record_of(rt, proposal.id).get("edit") or {}
    try:
        return draft(
            proposal,
            evidence_of(rt, proposal) if evidence is None else evidence,
            reader(rt),
            kind=edit.get("kind"),
            lesson=edit.get("lesson"),
            team=rt.ws.team,
            at=_now(),
        )
    except DraftError as error:
        raise CliError(
            str(error), ExitCode.INVALID, hint="`agentic learn edit <id> --kind` retargets it"
        ) from None


def target_scope(rt: Runtime, proposal: Proposal, evidence: list[Evidence]) -> str:
    try:
        return draft_for(rt, proposal, evidence).scope
    except CliError:
        return "repo"


# ── decisions (standalone) ──────────────────────────────────────────────────


def check_owner(rt: Runtime, made: Draft, what: str, warn: Callable[[str], None]) -> str:
    user = rt.known_user()
    if not may_decide(made.scope, made.owner, user, None):
        raise CliError(
            f"only {made.owner} may {what} this personal proposal (it would change their own "
            f"~/.agentic); you are {user}",
            ExitCode.REFUSED,
            hint=f"`agentic learn endorse {made.proposal_id}` backs it instead",
        )
    if made.scope != "personal":
        warn(NO_RBAC)
    return user


def decide(rt: Runtime, pid: str, action: str, warn: Callable[[str], None]) -> Proposal:
    proposal = get(rt, pid)
    _allowed(proposal, action)
    user = check_owner(rt, draft_for(rt, proposal), action, warn)
    try:
        return store(rt).decide(pid, action, user, _now())
    except DecisionError as error:
        raise CliError(str(error), ExitCode.REFUSED) from None


def _allowed(proposal: Proposal, action: str) -> None:
    try:
        next_status(proposal.status, action)
    except DecisionError as error:
        raise CliError(str(error), ExitCode.REFUSED) from None


def edit(rt: Runtime, pid: str, kind: str | None, lesson: str | None, warn) -> Draft:
    proposal = get(rt, pid)
    if proposal.status not in ("pending", "deferred"):
        raise CliError(f"a {proposal.status} proposal cannot be edited", ExitCode.REFUSED)
    record = record_of(rt, pid)
    check_owner(rt, draft_for(rt, proposal, edit=record.get("edit")), "edit", warn)
    change = {**(record.get("edit") or {})}
    change.update({k: v for k, v in (("kind", kind), ("lesson", lesson)) if v})
    made = draft_for(rt, proposal, edit=change)
    store(rt).save_change(pid, {**record, "edit": change})
    return made


def endorse(rt: Runtime, pid: str) -> Proposal:
    get(rt, pid)
    return store(rt).endorse(pid, rt.known_user())


def propose_from(rt: Runtime, feedback_id: str, agent: str | None) -> Proposal:
    """An explicit request to teach one correction now (spec §24 step 3)."""
    found = learning.all_states(rt)
    state = found.get(feedback_id)
    if state is None:
        raise CliError(
            f"there is no feedback record {feedback_id} in this repository's runs",
            ExitCode.INVALID,
            hint="`agentic learn feedback` lists them",
        )
    agents = state.attribution.agents if state.attribution else []
    if not agents:
        raise CliError(
            f"{feedback_id} names no agent that learns from it",
            ExitCode.INVALID,
            hint=f"`agentic learn attribute {feedback_id} --agent <name> --reason ...` first",
        )
    chosen = agent or agents[0]
    if chosen not in agents:
        raise CliError(f"{feedback_id} teaches {', '.join(agents)}, not {chosen}", ExitCode.USAGE)
    lesson = state.lesson or state.record.text
    scope = f"repo:{state.record.repo or 'local'}"
    made = Proposal(
        id=proposal_id(chosen, scope, f"explicit {feedback_id} {lesson}"),
        agent=chosen,
        scope=scope,
        lesson=lesson,
        evidence=(feedback_id,),
        sources=(state.record.source,),
        actors=(state.record.actor,),
        created_at=_now(),
    )
    try:
        held = {p.id: p for p in store(rt).proposals()}
        if made.id in held:
            return held[made.id]
        store(rt).save([made])
    except StoreError as error:
        raise _store_errors(error) from None
    return made


# ── apply and revert ────────────────────────────────────────────────────────


def approve(rt: Runtime, pid: str, warn: Callable[[str], None]) -> tuple[Proposal, dict]:
    """Approve (owner only), then apply: the only path to a changed skill, note or file."""
    proposal = get(rt, pid)
    _allowed(proposal, "approve")
    made = draft_for(rt, proposal)
    user = check_owner(rt, made, "approve", warn)
    if made.problems:
        raise CliError(
            "this change is not instruction-only: " + "; ".join(made.problems),
            ExitCode.INVALID,
            hint=f"`agentic learn edit {pid} --lesson ...` rewrites it",
        )
    if made.root == "policy":
        raise CliError(
            "team learnings land as policy-repo pull requests, which this version does not "
            "open yet",
            ExitCode.REFUSED,
            hint=f"`agentic learn edit {pid} --kind repo-memory` makes it a repo change",
        )
    approved = store(rt).decide(pid, "approve", user, _now())
    applied = _apply(rt, made, user, warn)
    record = record_of(rt, pid)
    store(rt).save_change(
        pid, {**record, "draft": made.model_dump(mode="json"), "applied": applied}
    )
    return store(rt).decide(approved.id, "apply", user, _now()), applied


def _apply(rt: Runtime, made: Draft, user: str, warn: Callable[[str], None]) -> dict:
    if made.root == "home":
        return apply_personal(rt, made, user)
    return apply_shared(rt, made, warn)


def apply_personal(rt: Runtime, made: Draft, user: str) -> dict:
    changes = PersonalChanges(rt.ws.home)
    why = f"agentic learn approve {made.proposal_id}: {made.lesson}"
    try:
        changes.apply(made.proposal_id, made.path, made.after, by=user, why=why)
        case = dump_yaml(made.eval_case)
        changes.apply(made.proposal_id + EVAL_PART, made.eval_path, case, by=user, why=why)
    except LearningApplyError as error:
        raise CliError(str(error), ExitCode.REFUSED) from None
    return {"kind": "personal", "paths": [made.path, made.eval_path], "at": _now().isoformat()}


def _title(made: Draft, verb: str = "Learning") -> str:
    lesson = made.lesson if len(made.lesson) <= 60 else made.lesson[:57] + "..."
    return f"{verb} ({made.agent}): {lesson}"


def _body(made: Draft, user: str) -> str:
    evidence = "\n".join(
        f"- {e.given_at.date()} {e.signal} by {e.actor}: {' '.join(e.text.split())[:200]}"
        for e in made.evidence
    )
    return (
        f"Learning `{made.proposal_id}` for the **{made.agent}** agent, approved by {user} "
        f"with `agentic learn approve` (scope: {made.scope}, {made.owner}).\n\n"
        f"**Lesson:** {made.lesson}\n\n"
        f"**Evidence** (the corrections that triggered it):\n{evidence or '- (none recorded)'}\n\n"
        f"**Change:**\n\n```diff\n{made.diff[:20000]}```\n\n"
        f"**Regression case:** `{made.eval_path}` (the corrected example, M1-63 format).\n\n"
        f"Undo it with `agentic learn revert {made.proposal_id}` (a revert pull request)."
    )


def _branch_off_default(
    rt: Runtime,
    files_for: Callable[[str], dict[str, str] | None],
    branch: str,
    title: str,
    body: str,
    warn: Callable[[str], None] | None = None,
) -> dict | None:
    with connect(rt.ws.repo, rt.ws.settings, remote=REMOTE) as conn, conn.errors():
        provider = conn.provider
        base = provider.default_branch(conn.repo)
        base_rev = _base(rt.ws.repo, conn, base, warn or (lambda m: _LOG.warning("%s", m)))
        files = files_for(base_rev)
        if files is None:
            return None
        message = f"{title}\n\nOpened with agentic learn (the learning loop, M3-16)."
        name, commit = repo_setup.commit_on_branch(
            rt.ws.repo, dict(files), message, branch=branch, base=base_rev
        )
        push_branch(rt.ws.repo, conn.remote, name, secrets=[conn.token])
        pull = provider.open_pull_request(conn.repo, head=name, base=base, title=title, body=body)
    return {
        "kind": "pull-request",
        "number": pull.number,
        "url": pull.url,
        "branch": name,
        "commit": commit,
        "base": base,
        "repo": f"{conn.repo.host}/{conn.repo.slug}",
        "at": _now().isoformat(),
    }


def _base(repo, conn, base: str, warn: Callable[[str], None]) -> str:
    """The commit of `base` on the remote: fetched now; when the fetch fails, the
    remote-tracking branch from the last fetch, said so (as `skills submit` does)."""
    try:
        return fetch_branch(repo, conn.remote, base, BASE_REF, secrets=[conn.token])
    except HostingError as error:
        tracking = f"refs/remotes/{conn.remote}/{base}"
        try:
            found = rev_parse(repo, tracking)
        except HostingError:
            raise CliError(
                f"could not read {base} from {conn.remote}: {error}", ExitCode.ERROR
            ) from None
        warn(f"could not fetch {base} ({error}); building on {conn.remote}/{base} as last fetched")
        return found


def apply_shared(rt: Runtime, made: Draft, warn: Callable[[str], None] | None = None) -> dict:
    """A pull request with the change re-rendered on the default branch's file (so it
    changes only the learned block, whatever the local checkout holds)."""
    user = rt.known_user()

    def files(base_rev: str) -> dict[str, str]:
        current = head_text(rt.ws.repo, made.path, base_rev)
        after = render_change(made.kind, current, made.lesson, made.agent, made.proposal_id)
        return {made.path: after, made.eval_path: dump_yaml(made.eval_case)}

    branch = BRANCH.format(id=made.proposal_id)
    opened = _branch_off_default(rt, files, branch, _title(made), _body(made, user), warn)
    if opened is None:  # files() always has the change; kept for the type
        raise CliError("there was nothing to propose", ExitCode.ERROR)
    return {**opened, "path": made.path}


def revert(rt: Runtime, pid: str, warn: Callable[[str], None]) -> tuple[Proposal, dict]:
    proposal = get(rt, pid)
    _allowed(proposal, "revert")
    record = record_of(rt, pid)
    made = Draft.model_validate(record["draft"])
    user = check_owner(rt, made, "revert", warn)
    applied = record.get("applied") or {}
    if applied.get("kind") == "personal":
        try:
            PersonalChanges(rt.ws.home).revert(pid, by=user)
        except LearningApplyError as error:
            raise CliError(str(error), ExitCode.REFUSED) from None
        reverted: dict = {"kind": "personal", "paths": applied.get("paths", [])}
    else:
        reverted = revert_shared(rt, made, applied, warn)
    reverted["at"] = _now().isoformat()
    store(rt).save_change(pid, {**record, "reverted": reverted})
    return store(rt).decide(pid, "revert", user, _now()), reverted


def revert_shared(rt: Runtime, made: Draft, applied: dict, warn) -> dict:
    """A revert pull request: the default branch's file without the learned block."""
    path = applied.get("path") or made.path

    def files(base_rev: str) -> dict[str, str] | None:
        current = head_text(rt.ws.repo, path, base_rev)
        if current is None:
            return None
        restored = strip_change(current, made.proposal_id)
        return None if restored == current else {path: restored}

    title = _title(made, "Revert learning")
    body = (
        f"Reverts learning `{made.proposal_id}` (pull request #{applied.get('number')}) for the "
        f"**{made.agent}** agent: its block is taken out of `{path}`, which then holds what it "
        "held before. The regression case stays: it records the correction."
    )
    opened = _branch_off_default(
        rt, files, BRANCH.format(id=f"{made.proposal_id}-revert"), title, body, warn
    )
    if opened is None:
        warn(
            f"the learning is not on the default branch (pull request #{applied.get('number')} "
            "was not merged): close that pull request on the host"
        )
        return {"kind": "not-merged", "number": applied.get("number")}
    return {**opened, "path": path}


# ── the commands' entry points (standalone here, the control plane in org mode) ──


def _org(rt: Runtime) -> bool:
    return bool(rt.ws.settings.get("org"))


def proposal_row(p: Proposal) -> dict:
    return {
        "id": p.id,
        "agent": p.agent,
        "scope": p.scope,
        "lesson": p.lesson,
        "evidence": list(p.evidence),
        "status": p.status,
        "created_at": p.created_at.isoformat(),
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "decided_by": p.decided_by,
        "endorsements": list(p.endorsements),
    }


def show(rt: Runtime, pid: str) -> dict:
    if _org(rt):
        return org_show(rt, pid)
    proposal = get(rt, pid)
    record = record_of(rt, pid)
    made = (
        Draft.model_validate(record["draft"])
        if record.get("draft") and proposal.status in ("applied", "reverted")
        else draft_for(rt, proposal, edit=record.get("edit"))
    )
    return {
        **proposal_row(proposal),
        "draft": made.model_dump(mode="json"),
        "applied": record.get("applied"),
        "reverted": record.get("reverted"),
    }


def approve_any(rt: Runtime, pid: str, warn: Callable[[str], None]) -> dict:
    if _org(rt):
        return org_approve(rt, pid, warn)
    proposal, applied = approve(rt, pid, warn)
    return {**proposal_row(proposal), "applied": applied}


def revert_any(rt: Runtime, pid: str, warn: Callable[[str], None]) -> dict:
    if _org(rt):
        return org_revert(rt, pid, warn)
    proposal, reverted = revert(rt, pid, warn)
    return {**proposal_row(proposal), "reverted": reverted}


def decide_any(rt: Runtime, pid: str, action: str, reason: str, warn) -> dict:
    if _org(rt):
        return org_decide(rt, pid, action, reason)
    return proposal_row(decide(rt, pid, action, warn))


def edit_any(rt: Runtime, pid: str, kind: str | None, lesson: str | None, warn) -> dict:
    if _org(rt):
        made = org_edit(rt, pid, kind, lesson)
    else:
        made = edit(rt, pid, kind, lesson, warn)
    return made.model_dump(mode="json")


def endorse_any(rt: Runtime, pid: str) -> dict:
    if _org(rt):
        return _client(rt, lambda c: c.endorse_proposal(pid)).model_dump(mode="json")
    return proposal_row(endorse(rt, pid))


def propose_any(rt: Runtime, feedback_id: str, agent: str | None) -> dict:
    if _org(rt):
        info = _client(rt, lambda c: c.propose_from(feedback_id, agent))
        return info.model_dump(mode="json")
    return proposal_row(propose_from(rt, feedback_id, agent))


# ── org mode: the control plane decides who may act; the change is made here ──


def _client(rt: Runtime, call: Callable):
    client = rt.control_plane()
    if client is None:
        raise CliError(
            "this needs org mode with a control plane",
            ExitCode.REFUSED,
            hint="run `agentic login --for control-plane`",
        )
    with client:
        return call(client)


def _org_evidence(detail) -> list[Evidence]:
    return [
        Evidence(
            feedback_id=item.id,
            signal=item.signal,
            actor=item.actor,
            given_at=item.given_at,
            text=item.text,
            run_id=item.run_id,
            repo=item.repo,
        )
        for item in detail.feedback
    ]


def _org_proposal(detail) -> Proposal:
    return Proposal(
        id=detail.id,
        agent=detail.agent,
        scope=detail.scope,
        lesson=detail.lesson,
        evidence=tuple(detail.evidence),
        sources=(),
        actors=tuple(detail.actors),
        created_at=detail.created_at,
        status=detail.status,
        decided_at=detail.decided_at,
        decided_by=detail.decided_by,
        endorsements=tuple(detail.endorsements),
    )


def _org_draft(rt: Runtime, detail, edit: dict | None = None) -> Draft:
    proposal = _org_proposal(detail)
    change = {"kind": detail.kind, "lesson": detail.edited_lesson}
    change.update({k: v for k, v in (edit or {}).items() if v})
    try:
        made = draft(
            proposal,
            _org_evidence(detail),
            reader(rt),
            kind=change.get("kind"),
            lesson=change.get("lesson"),
            team=detail.team,
            at=_now(),
        )
    except DraftError as error:
        raise CliError(str(error), ExitCode.INVALID) from None
    if made.scope == "personal" and detail.owner:
        made = made.model_copy(update={"owner": detail.owner})
    return made


def org_list(rt: Runtime, status: str | None) -> list[dict]:
    found = _client(rt, lambda c: c.proposals(status=status))
    return [
        {**item.model_dump(mode="json"), "evidence": len(item.evidence), "target": item.target}
        for item in found
    ]


def org_show(rt: Runtime, pid: str) -> dict:
    detail = _client(rt, lambda c: c.proposal(pid))
    if detail.draft and detail.status in ("applied", "reverted"):
        made = Draft.model_validate(detail.draft)
    else:
        made = _org_draft(rt, detail)
    return {
        **detail.model_dump(mode="json", exclude={"feedback"}),
        "draft": made.model_dump(mode="json"),
    }


def org_decide(rt: Runtime, pid: str, action: str, reason: str) -> dict:
    info = _client(rt, lambda c: c.decide_proposal(pid, action, reason))
    return info.model_dump(mode="json")


def org_edit(rt: Runtime, pid: str, kind: str | None, lesson: str | None) -> Draft:
    detail = _client(rt, lambda c: c.proposal(pid))
    made = _org_draft(rt, detail, {"kind": kind, "lesson": lesson})  # refused here if invalid
    _client(rt, lambda c: c.edit_proposal(pid, kind, lesson))
    return made


def org_approve(rt: Runtime, pid: str, warn: Callable[[str], None]) -> dict:
    """The control plane approves (owner only, audited); the change is then made from this
    machine and reported back (`applied`, audited)."""
    detail = _client(rt, lambda c: c.proposal(pid))
    made = _org_draft(rt, detail)
    if made.problems:
        raise CliError(
            "this change is not instruction-only: " + "; ".join(made.problems), ExitCode.INVALID
        )
    if made.root == "policy":
        raise CliError(
            "team learnings land as policy-repo pull requests, which this version does not "
            "open yet",
            ExitCode.REFUSED,
        )
    _client(rt, lambda c: c.decide_proposal(pid, "approve", "", made.model_dump(mode="json")))
    user = rt.known_user()
    applied = _apply(rt, made, user, warn)
    store(rt).save_change(pid, {"draft": made.model_dump(mode="json"), "applied": applied})
    info = _client(rt, lambda c: c.report_applied(pid, applied))
    return {**info.model_dump(mode="json"), "applied": applied}


def org_revert(rt: Runtime, pid: str, warn: Callable[[str], None]) -> dict:
    detail = _client(rt, lambda c: c.proposal(pid))
    if detail.status != "applied":
        raise CliError(f"a {detail.status} proposal cannot be reverted", ExitCode.REFUSED)
    made = Draft.model_validate(detail.draft)
    applied = detail.applied_ref or {}
    if applied.get("kind") == "personal":
        try:
            PersonalChanges(rt.ws.home).revert(pid, by=rt.known_user())
        except LearningApplyError as error:
            raise CliError(str(error), ExitCode.REFUSED) from None
        reverted: dict = {"kind": "personal", "paths": applied.get("paths", [])}
    else:
        reverted = revert_shared(rt, made, applied, warn)
    reverted["at"] = _now().isoformat()
    info = _client(rt, lambda c: c.report_reverted(pid, reverted))
    return {**info.model_dump(mode="json"), "reverted": reverted}


def pending_notice(out: Output, rt: Runtime) -> None:
    """The end of a run: how many proposals wait for a decision (standalone)."""
    if rt.ws.settings.get("org"):
        return
    try:
        waiting = [p for p in store(rt).proposals() if p.status in ("pending", "deferred")]
    except Exception:  # a notice never stops a command
        return
    if waiting:
        out.warn(
            f"{len(waiting)} learning proposal(s) wait for a decision: `agentic learn list`, "
            "then `agentic learn show <id>`"
        )


__all__ = [
    "approve",
    "decide",
    "draft_for",
    "edit",
    "endorse",
    "evidence_of",
    "pending_notice",
    "propose_from",
    "proposals",
    "revert",
    "store",
]
