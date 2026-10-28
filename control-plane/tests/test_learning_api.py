"""The learning loop in the control plane (M3-11..M3-13): each run's feedback journal is
ingested all or nothing, per tenant; records are read by their author and the team (D30);
a person corrects an attribution (audited); the third similar correction makes a proposal,
and a rejected lesson is not proposed again without new evidence."""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

import pytest
from apiclient import member
from sqlalchemy import func, select, update

from agentic.learning.records import (
    CAPTURED,
    MAX_LESSON,
    Attribution,
    FeedbackRecord,
    Learner,
    captured_fields,
    feedback_id,
)
from agentic_cp.audit import read_records
from agentic_cp.learning.store import learning_settings
from agentic_cp.tables import feedback_events, learning_proposals
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons import ledger as ledger_module
from sdlc_commons.feedback import FeedbackJournal
from sdlc_commons.ledger import RunIdentity, RunLedger

pytestmark = pytest.mark.anyio

POLICY = "git:" + "c" * 40
RETRY = [
    "Wrap outbound HTTP calls in the with_retry() helper.",
    "Use the with_retry helper around HTTP calls.",
    "HTTP calls should be wrapped with with_retry().",
    "Wrap the HTTP call in with_retry, as everywhere else.",
    "Remember the with_retry() helper for HTTP calls.",
    "with_retry() must wrap every outbound HTTP call.",
]


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
async def dev(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    return token


async def open_run(api, token, tmp_path: Path, run_id="run-1", team="payments"):
    ledger = RunLedger(tmp_path / f"ledger-{run_id}", run_id)
    ledger.open(
        identity=RunIdentity(team=team, policy_version=POLICY),
        kind="feature",
        project="demo",
        owner=team,
        criticality="low",
    )
    entries = ledger.events()
    ledger.release()
    reply = await api.post(f"/v1/runs/{run_id}/events", token, {"entries": entries})
    assert reply.status_code == 200, reply.text


def record(api, text: str, signal="pr-review", run_id="run-1", n=0, **extra) -> FeedbackRecord:
    source = extra.pop("source", f"pr:github.com/acme/app#7:comment:{signal}-{n}-{text}")
    return FeedbackRecord(
        id=feedback_id(run_id, signal, source),
        signal=signal,
        run_id=run_id,
        actor="alice",
        given_at=api.clock.now - timedelta(hours=1),
        text=text,
        source=source,
        repo="github.com/acme/app",
        **extra,
    )


def rules(*agents: str) -> Attribution:
    return Attribution(
        learners=tuple(Learner(agent=a, confidence=1.0) for a in agents), method="rules"
    )


class Journal:
    """A run's real feedback journal (the CLI writes the same), in a scratch folder."""

    def __init__(self, tmp_path: Path, run_id="run-1") -> None:
        self.journal = FeedbackJournal(tmp_path / f"journal-{run_id}", run_id)
        self.sent = 0

    def capture(self, rec: FeedbackRecord, attribution: Attribution | None, lesson: str):
        fields = captured_fields(rec, attribution, lesson)
        self.journal.append(CAPTURED, fields, row_id=rec.id, clock=lambda: rec.given_at)

    def pending(self) -> list[dict]:
        entries = self.journal.entries()[self.sent :]
        self.sent += len(entries)
        return entries


async def send(api, token, entries, run_id="run-1"):
    return await api.post(f"/v1/runs/{run_id}/feedback", token, {"entries": entries})


async def stored(api, tenant_id, table=feedback_events) -> int:
    async with tenant_tx(api.db.app, tenant_id) as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def audit_events(api, tenant_id, prefix="learning.") -> list:
    async with tenant_tx(api.db.app, tenant_id) as conn:
        return [r for r in await read_records(conn) if r.event.startswith(prefix)]


async def test_feedback_ingest_is_all_or_nothing_and_replay_safe(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    journal.capture(record(api, "Wrap this call.", path="a.py", line=3), rules("developer"), "x")
    journal.capture(record(api, "And this one.", path="a.py", line=9), rules("developer"), "y")
    entries = journal.pending()

    tampered = copy.deepcopy(entries)
    tampered[1]["text"] = "edited after hashing"
    refused = await send(api, dev, tampered)
    assert refused.status_code == 422 and refused.json()["code"] == "ingest.chain_broken"
    assert await stored(api, api.a.id) == 0

    gap = await send(api, dev, entries[1:])
    assert gap.status_code == 409 and gap.json()["code"] == "ingest.gap"

    first = await send(api, dev, entries)
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 2
    again = await send(api, dev, entries)
    assert again.json() == {"accepted": 0, "duplicates": 2, "head": first.json()["head"]}
    assert await stored(api, api.a.id) == 2


async def test_feedback_for_unknown_run_is_404(api, dev, tmp_path):
    journal = Journal(tmp_path, "never-sent")
    journal.capture(record(api, "x", run_id="never-sent"), None, "x")
    reply = await send(api, dev, journal.pending(), run_id="never-sent")
    assert reply.status_code == 404 and reply.json()["code"] == "not_found"


async def test_a_record_of_another_run_is_refused(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    journal.capture(record(api, "x", run_id="run-2"), None, "x")
    reply = await send(api, dev, journal.pending())
    assert reply.status_code == 422 and "run-2" in reply.text


async def test_someone_elses_run_needs_run_cancel_any(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    other, _ = await member(api, api.a, "dev2@a.example.test", "developer", "payments")
    journal = Journal(tmp_path)
    journal.capture(record(api, "x"), None, "x")
    reply = await send(api, other, journal.pending())
    assert reply.status_code == 403


async def test_each_signal_reaches_the_control_plane(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    journal.capture(
        record(
            api,
            "use the queue",
            "gate-rejection",
            gate="design-review",
            kind="design",
            agent="design@1.0.0",
            source="gate:design-review:reject:1",
        ),
        rules("design"),
        "Reuse the existing queue.",
    )
    journal.capture(
        record(
            api,
            "hourly",
            "gate-edit",
            kind="requirements",
            agent="business-analyst@1.0.0",
            diff="-nightly\n+hourly\n",
            source="gate:req:edit:1",
        ),
        rules("business-analyst"),
        "Exports run hourly.",
    )
    inline = record(api, "Wrap this call.", path="billing/export.py", line=42, n=1)
    journal.capture(inline, rules("developer", "code-review"), "Wrap calls in with_retry().")
    miss = inline.model_copy(
        update={
            "id": feedback_id("run-1", "review-miss", inline.source),
            "signal": "review-miss",
            "agent": "code-review@1.0.0",
            "kind": "review",
        }
    )
    journal.capture(miss, rules("code-review"), "Flag unwrapped HTTP calls.")
    assert (await send(api, dev, journal.pending())).status_code == 200

    listed = (await api.get("/v1/learning/feedback?run_id=run-1", dev)).json()
    assert sorted(item["signal"] for item in listed) == [
        "gate-edit",
        "gate-rejection",
        "pr-review",
        "review-miss",
    ]
    by_signal = {item["signal"]: item for item in listed}
    assert by_signal["gate-rejection"]["attribution"]["learners"][0]["agent"] == "design"
    assert by_signal["pr-review"]["path"] == "billing/export.py"
    assert by_signal["gate-edit"]["lesson"] == "Exports run hourly."
    assert all(item["author"] == "dev@a.example.test" for item in listed)
    captured = await audit_events(api, api.a.id, "learning.feedback_captured")
    assert len(captured) == 4 and {r.team for r in captured} == {"payments"}


async def test_author_and_team_see_feedback_others_do_not(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    journal.capture(record(api, "Wrap this call.", path="a.py", line=1), rules("developer"), "x")
    assert (await send(api, dev, journal.pending())).status_code == 200
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    peer, _ = await member(api, api.a, "peer@a.example.test", "developer", "payments")
    other_lead, _ = await member(api, api.a, "olead@a.example.test", "team-lead", "billing")

    async def count(token) -> int:
        reply = await api.get("/v1/learning/feedback", token)
        assert reply.status_code == 200, reply.text
        return len(reply.json())

    assert await count(dev) == 1  # its author
    assert await count(lead) == 1  # the team's lead (dashboard.view.team)
    assert await count(api.a.admin_token) == 1  # an org-wide grant
    assert await count(peer) == 0  # another developer of the team
    assert await count(other_lead) == 0  # another team's lead
    assert await count(api.b.admin_token) == 0  # another tenant


async def test_attribution_correction_is_audited_and_permissioned(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    rec = record(api, "Hmm, not like this.")
    journal.capture(rec, None, "Not like this.")
    assert (await send(api, dev, journal.pending())).status_code == 200
    path = f"/v1/learning/feedback/{rec.id}/attribution"
    body = {"agents": ["design"], "reason": "an architecture point"}
    peer, _ = await member(api, api.a, "peer@a.example.test", "developer", "payments")
    approver, _ = await member(api, api.a, "appr@a.example.test", "approver", "payments")
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")

    assert (await api.put(path, peer, body)).status_code == 404  # cannot even read it
    assert (await api.put(path, approver, body)).status_code == 403  # reads, cannot correct
    assert (await api.put(path, api.b.admin_token, body)).status_code == 404
    assert await audit_events(api, api.a.id, "learning.attribution_corrected") == []

    done = await api.put(path, lead, body)
    assert done.status_code == 200, done.text
    found = done.json()
    assert found["attribution"]["method"] == "human"
    assert [x["agent"] for x in found["attribution"]["learners"]] == ["design"]
    assert found["corrected_by"] == "lead@a.example.test"
    mine = await api.put(path, dev, {"agents": ["developer"], "reason": "mine after all"})
    assert mine.status_code == 200
    audited = await audit_events(api, api.a.id, "learning.attribution_corrected")
    assert [(r.actor, r.after["agents"]) for r in audited] == [
        ("lead@a.example.test", ["design"]),
        ("dev@a.example.test", ["developer"]),
    ]
    assert audited[0].before == {"agents": []} and audited[0].reason == "an architecture point"

    bad = await api.put(path, lead, {"agents": ["Not An Agent"], "reason": "x"})
    assert bad.status_code == 422


async def test_third_similar_correction_creates_a_proposal_on_ingest(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    for n, text in enumerate(RETRY[:2]):
        journal.capture(record(api, text, n=n, path="a.py", line=n + 1), rules("developer"), text)
    assert (await send(api, dev, journal.pending())).status_code == 200
    assert await stored(api, api.a.id, learning_proposals) == 0

    journal.capture(record(api, RETRY[2], n=2, path="a.py", line=9), rules("developer"), RETRY[2])
    assert (await send(api, dev, journal.pending())).status_code == 200
    proposals = (await api.get("/v1/learning/proposals", dev)).json()  # an evidence author
    assert len(proposals) == 1
    proposal = proposals[0]
    assert (proposal["agent"], proposal["scope"], proposal["status"]) == (
        "developer",
        "repo:github.com/acme/app",
        "pending",
    )
    assert len(proposal["evidence"]) == 3 and proposal["lesson"] in RETRY[:3]
    proposed = await audit_events(api, api.a.id, "learning.proposed")
    assert [r.target for r in proposed] == [f"proposal:{proposal['id']}"]
    peer, _ = await member(api, api.a, "peer@a.example.test", "developer", "payments")
    assert (await api.get("/v1/learning/proposals", peer)).json() == []

    journal.capture(record(api, RETRY[3], n=3, path="a.py", line=12), rules("developer"), RETRY[3])
    assert (await send(api, dev, journal.pending())).status_code == 200
    grown = (await api.get("/v1/learning/proposals", dev)).json()
    assert [(p["id"], len(p["evidence"])) for p in grown] == [(proposal["id"], 4)]
    assert len(await audit_events(api, api.a.id, "learning.proposed")) == 1


async def test_rejected_proposal_is_not_re_proposed_on_ingest(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    for n, text in enumerate(RETRY[:3]):
        journal.capture(record(api, text, n=n, path="a.py", line=n + 1), rules("developer"), text)
    assert (await send(api, dev, journal.pending())).status_code == 200
    async with tenant_tx(api.db.owner, api.a.id) as conn:  # L5 rejects; here by hand
        await conn.execute(
            update(learning_proposals).values(status="rejected", decided_at=api.clock.now)
        )
    api.clock.advance(timedelta(hours=2))
    for n, text in enumerate(RETRY[3:5], start=3):
        journal.capture(record(api, text, n=n, path="a.py", line=n + 1), rules("developer"), text)
    assert (await send(api, dev, journal.pending())).status_code == 200
    statuses = [p["status"] for p in (await api.get("/v1/learning/proposals", dev)).json()]
    assert statuses == ["rejected"]


def test_learning_settings_come_from_the_policy_version():
    stored = {
        "learning": {
            "threshold": 4,
            "window_days": 20,
            "similarity": "0.6",
            "teams": {"payments": {"threshold": 5, "window_days": 10}},
        }
    }
    assert learning_settings(stored, "payments").threshold == 5
    assert learning_settings(stored, "payments").window_days == 10
    assert learning_settings(stored, "billing").threshold == 4
    assert learning_settings(stored, "billing").similarity == 0.6
    assert learning_settings(None, "payments").threshold == 3  # a version without them


async def test_a_record_id_of_another_run_is_refused(api, dev, tmp_path):
    """A record's id is derived from its run, signal and source. A member who may send
    feedback for their own run cannot claim another run's record id and so drop it."""
    await open_run(api, dev, tmp_path)  # the victim's run
    attacker, _ = await member(api, api.a, "dev2@a.example.test", "developer", "payments")
    await open_run(api, attacker, tmp_path, run_id="run-2")
    genuine = record(api, "Wrap this call.", path="a.py", line=3)
    forged = record(api, "ATTACKER TEXT", run_id="run-2", source=genuine.source)
    forged = forged.model_copy(update={"id": genuine.id})
    theirs = Journal(tmp_path, "run-2")
    theirs.capture(forged, rules("developer"), "x")
    refused = await send(api, attacker, theirs.pending(), run_id="run-2")
    assert refused.status_code == 422 and genuine.id in refused.text

    mine = Journal(tmp_path)
    mine.capture(genuine, rules("developer"), "Wrap this call.")
    assert (await send(api, dev, mine.pending())).status_code == 200
    rows = (await api.get("/v1/learning/feedback", dev)).json()
    assert [(r["id"], r["run_id"]) for r in rows] == [(genuine.id, "run-1")]


async def test_a_lesson_is_capped_on_the_server(api, dev, tmp_path):
    await open_run(api, dev, tmp_path)
    journal = Journal(tmp_path)
    rec = record(api, "Wrap this call.", path="a.py", line=3)
    fields = {**captured_fields(rec, rules("developer"), "x"), "lesson": "y" * 5000}
    journal.journal.append(CAPTURED, fields, row_id=rec.id, clock=lambda: rec.given_at)
    assert (await send(api, dev, journal.pending())).status_code == 200
    rows = (await api.get("/v1/learning/feedback", dev)).json()
    assert len(rows[0]["lesson"]) <= MAX_LESSON
