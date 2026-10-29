"""Learning decisions in the control plane (M3-15, M3-16): only the owner of what a proposal
would change decides it (D30) - its one author for a personal proposal, a holder of
`agent.approve.repo` for a repo one - and a developer may endorse a shared proposal but
never approve it; nothing reaches `applied` without an approval (D29); every step is on the
audit chain; the approvers get a daily digest through the notifications outbox."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from apiclient import assign, member
from isolation import _feedback_in_b
from sqlalchemy import select, update
from test_learning_api import (  # noqa: F401 (fresh_identities is an autouse fixture)
    RETRY,
    Journal,
    audit_events,
    fresh_identities,
    open_run,
    record,
    rules,
    send,
)

from agentic_cp.learning.digest import KIND, queue_digest
from agentic_cp.tables import learning_proposals, notifications, repo_maintainers, repos
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

REPO = "github.com/acme/app"
DRAFT = {"kind": "repo-memory", "diff": "+ - Wrap HTTP calls in with_retry().\n"}
WHILE_APPLIED = [  # alike each of RETRY[:3], whichever of them seeds the cluster
    "Remember the with_retry() helper for HTTP calls.",
    "Wrap HTTP calls with the with_retry() helper.",
    "Use the with_retry() helper for outbound HTTP calls.",
]


@pytest.fixture
async def dev(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    return token


@pytest.fixture
async def lead(api):
    token, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    return token


async def person(api, email: str, role: str | None = None, team: str = "payments"):
    user_id, token = await api.add_user(api.a, email)
    if role:
        assert (await assign(api, api.a.admin_token, email, role, team)).status_code == 201
    return user_id, token


async def corrections(api, token, tmp_path, count=3, run_id="run-1", agent="developer"):
    """`count` similar corrections of `agent` in one run, sent by the caller's CLI."""
    await open_run(api, token, tmp_path, run_id=run_id)
    journal = Journal(tmp_path, run_id)
    for n, text in enumerate(RETRY[:count]):
        journal.capture(
            record(api, text, n=n, run_id=run_id, path="a.py", line=n + 1), rules(agent), text
        )
    assert (await send(api, token, journal.pending(), run_id=run_id)).status_code == 200
    return journal


async def shared_proposal(api, authors: list, actors=("alice", "bob")) -> str:
    """A repo proposal whose evidence comes from more than one person (stored directly)."""
    pid = f"lp-{uuid.uuid4().hex[:20]}"
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            learning_proposals.insert().values(
                tenant_id=api.a.id,
                id=pid,
                team="payments",
                agent="developer",
                scope=f"repo:{REPO}",
                lesson="Wrap HTTP calls in with_retry().",
                evidence=[f"fb-{uuid.uuid4().hex[:20]}" for _ in range(3)],
                actors=list(actors),
                authors=authors,
            )
        )
    return pid


async def decide(api, token, pid, action, **body):
    return await api.post(
        f"/v1/learning/proposals/{pid}/decision", token, {"action": action, **body}
    )


async def status_of(api, pid) -> str:
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        found = await conn.execute(
            select(learning_proposals.c.status).where(learning_proposals.c.id == pid)
        )
        return found.scalar_one()


async def test_personal_proposal_is_approvable_by_its_user(api, dev, lead, tmp_path):
    await corrections(api, dev, tmp_path)
    (listed,) = (await api.get("/v1/learning/proposals", dev)).json()
    pid = listed["id"]
    assert (listed["target"], listed["owner"]) == ("personal", "dev@a.example.test")

    detail = (await api.get(f"/v1/learning/proposals/{pid}", dev)).json()
    assert sorted(f["text"] for f in detail["feedback"]) == sorted(RETRY[:3])  # the evidence
    assert [f["id"] for f in detail["feedback"]] == detail["evidence"]  # in its order

    for other in (lead, api.a.admin_token):  # no permission stands in for its user
        refused = await decide(api, other, pid, "approve")
        assert refused.status_code == 403 and refused.json()["code"] == "rbac.forbidden"
    assert await status_of(api, pid) == "pending"

    approved = await decide(api, dev, pid, "approve", draft=DRAFT)
    assert approved.status_code == 200, approved.text
    assert (approved.json()["status"], approved.json()["decided_by"]) == (
        "approved",
        "dev@a.example.test",
    )
    applied = await api.post(
        f"/v1/learning/proposals/{pid}/applied", dev, {"ref": {"kind": "personal"}}
    )
    assert applied.json()["status"] == "applied"
    assert (await api.get(f"/v1/learning/proposals/{pid}", dev)).json()["draft"] == DRAFT
    reverted = await api.post(
        f"/v1/learning/proposals/{pid}/reverted", dev, {"ref": {"kind": "personal"}}
    )
    assert reverted.json()["status"] == "reverted"
    events = [r.event for r in await audit_events(api, api.a.id)]
    assert events[-3:] == ["learning.approved", "learning.applied", "learning.reverted"]


async def test_developer_cannot_approve_a_shared_proposal(api, dev, lead):
    dev_id, _ = await person(api, "dev2@a.example.test")
    peer_id, peer = await person(api, "peer@a.example.test", "developer")
    pid = await shared_proposal(api, [dev_id, peer_id])

    refused = await decide(api, peer, pid, "approve")  # an author of its evidence, even
    assert refused.status_code == 403 and "endorse" in refused.json()["detail"]
    assert (await decide(api, peer, pid, "reject")).status_code == 403
    assert await status_of(api, pid) == "pending"

    approved = await decide(api, lead, pid, "approve", draft=DRAFT)  # agent.approve.repo
    assert approved.status_code == 200, approved.text
    assert approved.json()["target"] == "repo" and approved.json()["owner"] == f"repo:{REPO}"
    decided = await audit_events(api, api.a.id, "learning.approved")
    assert [(r.actor, r.target) for r in decided] == [("lead@a.example.test", f"proposal:{pid}")]


async def test_endorse_adds_weight_but_never_approves(api, dev):
    peer_id, peer = await person(api, "peer@a.example.test", "developer")
    other_id, _ = await person(api, "other@a.example.test")
    pid = await shared_proposal(api, [peer_id, other_id])

    endorsed = await api.post(f"/v1/learning/proposals/{pid}/endorse", peer, None)
    assert endorsed.status_code == 200, endorsed.text
    assert (endorsed.json()["status"], endorsed.json()["endorsements"]) == (
        "pending",
        ["peer@a.example.test"],
    )
    again = await api.post(f"/v1/learning/proposals/{pid}/endorse", peer, None)
    assert again.json()["endorsements"] == ["peer@a.example.test"]  # once per person
    assert len(await audit_events(api, api.a.id, "learning.endorsed")) == 1
    stranger, _ = await member(api, api.a, "x@a.example.test", "developer", "ledger")
    assert (
        await api.post(f"/v1/learning/proposals/{pid}/endorse", stranger, None)
    ).status_code == 404


async def test_nothing_is_applied_without_approve(api, lead):
    a_id, _ = await person(api, "a1@a.example.test")
    b_id, _ = await person(api, "b1@a.example.test")
    pid = await shared_proposal(api, [a_id, b_id])
    report = {"ref": {"kind": "pull-request", "number": 7}}

    early = await api.post(f"/v1/learning/proposals/{pid}/applied", lead, report)
    assert early.status_code == 409 and early.json()["code"] == "learning.status"
    assert (await decide(api, lead, pid, "defer")).json()["status"] == "deferred"
    deferred = await api.post(f"/v1/learning/proposals/{pid}/applied", lead, report)
    assert deferred.status_code == 409
    assert (await decide(api, lead, pid, "reject", reason="not how we do it")).status_code == 200
    assert (
        await api.post(f"/v1/learning/proposals/{pid}/applied", lead, report)
    ).status_code == 409
    assert (await decide(api, lead, pid, "approve")).status_code == 409  # rejected stays so
    assert await status_of(api, pid) == "rejected"
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        row = (
            await conn.execute(select(learning_proposals).where(learning_proposals.c.id == pid))
        ).one()
    assert row.applied_at is None and row.applied_ref is None


async def test_rejected_lesson_is_not_proposed_again(api, dev, tmp_path):
    journal = await corrections(api, dev, tmp_path)
    (listed,) = (await api.get("/v1/learning/proposals", dev)).json()
    assert (await decide(api, dev, listed["id"], "reject")).status_code == 200
    api.clock.advance(timedelta(hours=2))
    for n, text in enumerate(RETRY[3:5], start=3):
        journal.capture(record(api, text, n=n, path="a.py", line=n + 1), rules("developer"), text)
    assert (await send(api, dev, journal.pending())).status_code == 200
    statuses = [p["status"] for p in (await api.get("/v1/learning/proposals", dev)).json()]
    assert statuses == ["rejected"]


async def test_edit_widens_the_target_but_never_narrows_it(api, lead):
    a_id, _ = await person(api, "a1@a.example.test")
    b_id, _ = await person(api, "b1@a.example.test")
    pid = await shared_proposal(api, [a_id, b_id])
    path = f"/v1/learning/proposals/{pid}/draft"

    narrower = await api.put(path, lead, {"kind": "personal-skill"})
    assert narrower.status_code == 422 and narrower.json()["code"] == "learning.kind"
    edited = await api.put(path, lead, {"kind": "team-skill", "lesson": "Use  with_retry()."})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["target"], edited.json()["edited_lesson"]) == (
        "team",
        "Use with_retry().",
    )
    assert edited.json()["owner"] == "team:payments"
    assert len(await audit_events(api, api.a.id, "learning.edited")) == 1


async def test_propose_from_one_feedback_record(api, dev, tmp_path):
    await corrections(api, dev, tmp_path, count=1)
    (fid,) = [f["id"] for f in (await api.get("/v1/learning/feedback", dev)).json()]

    made = await api.post("/v1/learning/proposals", dev, {"feedback_id": fid})
    assert made.status_code == 200, made.text
    assert (made.json()["evidence"], made.json()["status"]) == ([fid], "pending")
    again = await api.post("/v1/learning/proposals", dev, {"feedback_id": fid})
    assert again.json()["id"] == made.json()["id"]
    async with tenant_tx(api.db.app, api.a.id) as conn:  # its owner hears of it too
        mailed = await conn.execute(
            select(notifications.c.recipient).where(notifications.c.kind == KIND)
        )
        assert list(mailed.scalars()) == ["dev@a.example.test"]
    proposed = await audit_events(api, api.a.id, "learning.proposed")
    assert [r.target for r in proposed] == [f"proposal:{made.json()['id']}"]

    wrong = await api.post("/v1/learning/proposals", dev, {"feedback_id": fid, "agent": "design"})
    assert wrong.status_code == 422 and wrong.json()["code"] == "learning.agent"
    peer, _ = await member(api, api.a, "peer@a.example.test", "developer", "payments")
    assert (await api.post("/v1/learning/proposals", peer, {"feedback_id": fid})).status_code == 404
    in_b = await _feedback_in_b(api)  # another tenant's record is unknown here
    body = {"feedback_id": in_b["feedback_id"]}
    assert (await api.post("/v1/learning/proposals", api.a.admin_token, body)).status_code == 404


async def test_digest_is_queued_for_approvers(api, dev, lead, tmp_path):
    await corrections(api, dev, tmp_path)  # personal: its one author decides it
    maintainer_id, _ = await person(api, "maint@a.example.test")
    a_id, _ = await person(api, "a1@a.example.test")
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            repos.insert().values(
                tenant_id=api.a.id,
                repo_key=REPO,
                remote=f"https://{REPO}.git",
                team="payments",
                registered_by="admin",
            )
        )
        await conn.execute(
            repo_maintainers.insert().values(
                tenant_id=api.a.id, repo_key=REPO, user_id=maintainer_id, source_entry="* @m"
            )
        )
    await shared_proposal(api, [maintainer_id, a_id])
    async with tenant_tx(api.db.app, api.a.id) as conn:
        await queue_digest(conn, "payments", clock=api.clock)
        again = await queue_digest(conn, "payments", clock=api.clock)  # once a day
        mails = (
            await conn.execute(select(notifications).where(notifications.c.kind == KIND))
        ).all()
    assert again == 0
    to = {m.recipient: m for m in mails}
    assert {"dev@a.example.test", "maint@a.example.test", "lead@a.example.test"} <= set(to)
    assert "admin@a.example.test" not in to  # org-wide holders are not mailed every team's
    assert "a1@a.example.test" not in to  # an author of shared evidence does not decide it
    assert "personal" in to["dev@a.example.test"].body_text
    assert "repo" not in to["dev@a.example.test"].body_text.split("wait for your decision")[1]


async def test_detail_says_whether_the_caller_may_decide(api, lead):
    """Review F4: the CLI asks before it makes a change (resuming an approval, a revert),
    so the detail says whether the caller owns the proposal."""
    a_id, _ = await person(api, "a1@a.example.test")
    b_id, _ = await person(api, "b1@a.example.test")
    pid = await shared_proposal(api, [a_id, b_id])
    _, auditor = await person(api, "audit@a.example.test", "auditor")
    seen = await api.get(f"/v1/learning/proposals/{pid}", auditor)
    assert seen.status_code == 200, seen.text  # a team viewer reads it ...
    assert seen.json()["may_decide"] is False  # ... and may not decide it
    assert (await api.get(f"/v1/learning/proposals/{pid}", lead)).json()["may_decide"] is True


async def test_a_reverted_lesson_counts_only_corrections_after_the_revert(api, dev, tmp_path):
    """Review F6: the corrections made while a learning was in force (why it was reverted)
    do not propose it again at once: only corrections after the revert count."""
    journal = await corrections(api, dev, tmp_path)
    (listed,) = (await api.get("/v1/learning/proposals", dev)).json()
    pid = listed["id"]
    assert (await decide(api, dev, pid, "approve", draft=DRAFT)).status_code == 200
    ref = {"ref": {"kind": "personal"}}
    assert (await api.post(f"/v1/learning/proposals/{pid}/applied", dev, ref)).status_code == 200
    api.clock.advance(timedelta(hours=3))
    for n, text in enumerate(WHILE_APPLIED, start=3):
        journal.capture(record(api, text, n=n, path="a.py", line=n + 1), rules("developer"), text)
    assert (await send(api, dev, journal.pending())).status_code == 200
    api.clock.advance(timedelta(hours=2))
    reverted = await api.post(f"/v1/learning/proposals/{pid}/reverted", dev, ref)
    assert reverted.json()["decided_at"] > listed["created_at"]  # the revert is the decision
    api.clock.advance(timedelta(hours=3))
    late = "Wrap the HTTP call in with_retry, as everywhere else."
    journal.capture(record(api, late, n=6, path="a.py", line=9), rules("developer"), late)
    assert (await send(api, dev, journal.pending())).status_code == 200
    statuses = [p["status"] for p in (await api.get("/v1/learning/proposals", dev)).json()]
    assert statuses == ["reverted"]


async def test_the_digest_mails_waiting_proposals_every_day(api, lead):
    """Review F7: a proposal made after the day's mail went out is mailed the next day by
    the worker's digest job, not only when another proposal appears."""
    from agentic_cp.learning.digest import digest_job
    from agentic_cp.policy_sync.worker import EXTRA_JOBS

    assert digest_job in EXTRA_JOBS
    # the clock starts at the real time: begin at 08:00 UTC so "six hours later" is still today
    api.clock.now = (api.clock.now + timedelta(days=1)).replace(hour=8, minute=0, second=0)
    a_id, _ = await person(api, "a1@a.example.test")
    b_id, _ = await person(api, "b1@a.example.test")
    await shared_proposal(api, [a_id, b_id])  # in the morning: a proposal, the day's mail
    async with tenant_tx(api.db.app, api.a.id) as conn:
        assert await queue_digest(conn, "payments", clock=api.clock) == 1
    api.clock.advance(timedelta(hours=6))
    later = await shared_proposal(api, [a_id, b_id])  # in the afternoon: no mail today
    async with tenant_tx(api.db.app, api.a.id) as conn:
        assert await queue_digest(conn, "payments", clock=api.clock) == 0  # deduped today
    api.clock.advance(timedelta(days=1))
    await digest_job(api.db.app, None, api.a.id, api.settings, api.clock)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        mails = (
            await conn.execute(select(notifications).where(notifications.c.kind == KIND))
        ).all()
    lead_mails = [m for m in mails if m.recipient == "lead@a.example.test"]
    assert lead_mails and later in lead_mails[-1].body_text


async def test_authors_are_kept_when_a_proposal_grows(api, dev, tmp_path):
    """DF-737: an author of evidence that left the window still sees the proposal."""
    journal = await corrections(api, dev, tmp_path)
    (listed,) = (await api.get("/v1/learning/proposals", dev)).json()
    gone_id, gone = await person(api, "gone@a.example.test")
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(learning_proposals)
            .where(learning_proposals.c.id == listed["id"])
            .values(authors=learning_proposals.c.authors + [gone_id])
        )
    journal.capture(record(api, RETRY[3], n=3, path="a.py", line=12), rules("developer"), RETRY[3])
    assert (await send(api, dev, journal.pending())).status_code == 200
    seen = (await api.get("/v1/learning/proposals", gone)).json()
    assert [(p["id"], len(p["evidence"])) for p in seen] == [(listed["id"], 4)]


async def test_attribution_correction_reclusters_at_once(api, dev, tmp_path):
    """DF-733: moving a record to another agent re-clusters both at once."""
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    for n, text in enumerate(RETRY[:3]):
        agent = "developer" if n < 2 else "design"
        journal.capture(record(api, text, n=n, path="a.py", line=n + 1), rules(agent), text)
    assert (await send(api, dev, journal.pending())).status_code == 200
    assert (await api.get("/v1/learning/proposals", dev)).json() == []
    records = (await api.get("/v1/learning/feedback", dev)).json()
    moved = [f for f in records if f["attribution"]["learners"][0]["agent"] == "design"]
    corrected = await api.put(
        f"/v1/learning/feedback/{moved[0]['id']}/attribution",
        dev,
        {"agents": ["developer"], "reason": "it was the code"},
    )
    assert corrected.status_code == 200, corrected.text
    (proposal,) = (await api.get("/v1/learning/proposals", dev)).json()
    assert proposal["agent"] == "developer" and len(proposal["evidence"]) == 3
