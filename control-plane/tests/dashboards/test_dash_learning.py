"""The learning-effect view (M3-16): each applied learning's agent corrections and team runs
in the 30 days before and after it was applied, per tenant; a window without a run is
unmeasured (NULL), never a rate of 0."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from dash_seed import NOW, POLICY
from sqlalchemy import text

from agentic_cp.tables import feedback, learning_proposals, runs, users
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

APPLIED = NOW - timedelta(days=10)


async def _seed(conn, tenant, *, team="learning", agent="developer", runs_after=2) -> str:
    """One applied learning in `team` (a team the framework seed does not use)."""
    author = uuid.uuid4()
    await conn.execute(
        users.insert().values(
            tenant_id=tenant, id=author, email=f"dev-{author.hex[:6]}@a.test", kind="human"
        )
    )
    opened = [APPLIED - timedelta(days=d) for d in (20, 5)]  # two runs before
    opened += [APPLIED + timedelta(days=d + 1) for d in range(runs_after)]
    opened.append(APPLIED - timedelta(days=45))  # outside both windows
    for n, moment in enumerate(opened):
        await conn.execute(
            runs.insert().values(
                tenant_id=tenant,
                run_id=f"run-{team}-{n}-{author.hex[:6]}",
                team=team,
                started_by=author,
                policy_version=POLICY,
                policy_known=True,
                head_seq=0,
                opened_at=moment,
            )
        )
    given = [APPLIED - timedelta(days=d) for d in (15, 6, 2)]  # three corrections before
    given += [APPLIED + timedelta(days=3), APPLIED - timedelta(days=60)]  # one after, one old
    for n, moment in enumerate(given):
        await conn.execute(
            feedback.insert().values(
                tenant_id=tenant,
                id=f"fb-{uuid.uuid4().hex[:20]}",
                run_id=f"run-{team}-0-{author.hex[:6]}",
                team=team,
                repo="github.com/acme/app",
                signal="pr-review",
                actor="alice",
                given_at=moment,
                author_id=author,
                record={"text": f"wrap it {n}", "source": f"pr:x#{n}"},
                agents=[agent],
                lesson=f"Wrap it {n}.",
            )
        )
    pid = f"lp-{uuid.uuid4().hex[:20]}"
    await conn.execute(
        learning_proposals.insert().values(
            tenant_id=tenant,
            id=pid,
            team=team,
            agent=agent,
            scope="repo:github.com/acme/app",
            lesson="Wrap HTTP calls in with_retry().",
            status="applied",
            applied_at=APPLIED,
        )
    )
    await conn.execute(  # a learning never applied is not shown
        learning_proposals.insert().values(
            tenant_id=tenant,
            id=f"lp-{uuid.uuid4().hex[:20]}",
            team=team,
            agent=agent,
            scope="repo:github.com/acme/app",
            lesson="Never applied.",
        )
    )
    return pid


async def _rows(engine, tenant) -> list[dict]:
    async with tenant_tx(engine, tenant) as conn:
        found = await conn.execute(
            text(
                "select proposal_id, agent, corrections_before, runs_before, rate_before, "
                "corrections_after, runs_after, rate_after from dash_learning_effect "
                "where team = 'learning'"
            )
        )
        return [dict(row._mapping) for row in found]


async def test_learning_effect_view_shows_before_and_after(dash_db):
    db, seeded = dash_db
    async with tenant_tx(db.owner, seeded.a) as conn:
        pid = await _seed(conn, seeded.a)
    async with tenant_tx(db.owner, seeded.b) as conn:
        other = await _seed(conn, seeded.b, runs_after=0)

    assert await _rows(db.owner, seeded.a) == [
        {
            "proposal_id": pid,
            "agent": "developer",
            "corrections_before": 3,
            "runs_before": 2,
            "rate_before": Decimal("1.5000"),
            "corrections_after": 1,
            "runs_after": 2,
            "rate_after": Decimal("0.5000"),
        }
    ]
    (row,) = await _rows(db.owner, seeded.b)  # tenant b sees only its own
    assert row["proposal_id"] == other
    assert (row["runs_after"], row["rate_after"]) == (0, None)  # unmeasured, not 0
