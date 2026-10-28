"""The agent of a step, end to end (M2-37, review F1): a real run ledger written the way the
engine writes it, ingested through the API, read back by the dashboard views.

The engine records no agent identity on its step events today, so a step's agent is the
agent tag of its first gateway call; an agent the ledger does record wins."""

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from apiclient import member
from dash_seed import DEV, Call, tags
from sqlalchemy import text

from agentic_cp.dashboards.link import link_usage
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import AgentIdentity, RunIdentity, RunLedger

pytestmark = pytest.mark.anyio

GW = "GW-E2E"
RUN = "e2e-r1"
REVIEW_TAGGED = "code-review@1.0.0"


@pytest.fixture
def ledger():
    made = RunLedger(Path(tempfile.mkdtemp(prefix="cp-dash-agent-")), RUN)
    made.open(
        identity=RunIdentity(team="payments", policy_version="git:" + "c" * 40),
        kind="feature",
        project="demo",
        owner="payments",
        criticality="low",
    )
    yield made
    made.release()
    ledger_module._IDENTITIES.pop(RUN, None)


async def test_step_agent_comes_from_the_gateway_when_the_ledger_has_none(
    api, make_gateway, ledger
):
    # exactly the engine's calls (engine.py `_agent`): no agent identity on step events
    ledger.record("step_started", "develop", attempt=1)
    ledger.record("step_finished", "develop", outcome="completed", cost={"usd": "0.30"})
    # a ledger that does carry the agent (a later engine) is believed over the tag
    reviewer = AgentIdentity("code-review", "2.0.0", tier="premium")
    ledger.record("step_started", "review", agent=reviewer, attempt=1)
    ledger.record("step_finished", "review", agent=reviewer, outcome="completed")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    reply = await api.post(f"/v1/runs/{RUN}/events", dev, {"entries": ledger.events()})
    assert reply.status_code == 200, reply.text

    db = api.db
    async with db.owner.begin() as conn:
        await conn.execute(
            text("update tenants set gateway_tenant = :gw where id = :id"),
            {"gw": GW, "id": api.a.id},
        )
    gateway = await make_gateway(
        calls=(
            Call(GW, "0.120000", "payments", tags(RUN, "develop", DEV, "payments", tier="std")),
            Call(GW, "0.200000", "payments", tags(RUN, "develop", DEV, "payments", tier="std")),
            Call(GW, "0.050000", "payments", tags(RUN, "review", REVIEW_TAGGED, "payments")),
        )
    )
    async with db.owner.begin() as conn:
        await link_usage(conn, db.schema, gateway.db)

    async with tenant_tx(db.owner, api.a.id) as conn:
        steps = [
            dict(row._mapping)
            for row in await conn.execute(
                text(
                    "select step, agent, tier, agent_source from dash_step_runs "
                    "where run_id = :run order by seq"
                ),
                {"run": RUN},
            )
        ]
        agent_cost = {
            row.agent_key: row.reported_usd
            for row in await conn.execute(text("select * from dash_agent_cost"))
        }
    assert steps == [
        {"step": "develop", "agent": DEV, "tier": "std", "agent_source": "gateway"},
        {
            "step": "review",
            "agent": "code-review@2.0.0",
            "tier": "premium",
            "agent_source": "ledger",
        },
    ]
    assert agent_cost[DEV] == Decimal("0.30")
