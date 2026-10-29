"""The mail outbox (M2-07, Review Focus 1): a mail is queued in the transaction of the event
that causes it, once per dedupe key, and only in its own tenant; templates are plain text
plus a simple HTML part with every value escaped and no remote content."""

import pytest
from sqlalchemy import func, select

from agentic_cp.audit import read_records
from agentic_cp.notify.outbox import enqueue
from agentic_cp.notify.templates import KINDS, render
from agentic_cp.tables import notifications
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

LINK = "https://cp.acme.example/invite#aci_" + "x" * 20


def test_every_kind_renders_text_and_html():
    values = {
        "invite": {
            "email": "dev@acme.example",
            "inviter": "lead@acme.example",
            "role": "developer",
            "team_phrase": " in team payments",
            "link": LINK,
            "expires_at": "2026-10-26 09:00",
        },
        "gate.waiting": {
            "run_id": "run-1",
            "team": "payments",
            "gate": "design-approval",
            "role": "team-lead",
        },
        "budget.threshold": {
            "what": "Run run-1",
            "percent": "80",
            "spent_usd": "8.00",
            "budget_usd": "10.00",
            "period_phrase": "",
        },
        "auto_trip": {
            "team": "payments",
            "what": "spend rate",
            "value": "12.50 USD per hour",
            "threshold": "10.00 USD per hour",
            "window_minutes": "60",
            "reset": "5.00 USD per hour",
            "clear_phrase": "a person clears the pause switch",
        },
        "learning.digest": {
            "team": "payments",
            "count": "2",
            "proposals": "- lp-1 (developer, repo): Wrap HTTP calls in <with_retry>.",
        },
    }
    assert set(values) == set(KINDS)
    for kind, given in values.items():
        rendered = render(kind, given, tenant_name="Acme")
        assert rendered.subject and "\n" not in rendered.subject
        assert rendered.text.strip() and rendered.html.strip()
        assert "<img" not in rendered.html.lower()  # no tracking pixel, no remote image
        assert "$" not in rendered.text  # every placeholder filled


def test_html_escapes_values_and_links_only_what_the_mail_is_about():
    rendered = render(
        "invite",
        {
            "email": "<script>x</script>@acme.example",
            "inviter": "lead@acme.example",
            "role": "developer",
            "team_phrase": "",
            "link": LINK,
            "expires_at": "2026-10-26 09:00",
        },
        tenant_name="Acme & <Co>",
    )
    assert "<script>" not in rendered.html and "&lt;script&gt;" in rendered.html
    assert "Acme &amp; &lt;Co&gt;" in rendered.html
    assert rendered.html.count("http") == rendered.html.count(LINK.split("#")[0])
    assert LINK in rendered.text


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="kind"):
        render("party", {}, tenant_name="Acme")


async def _queue(api, tenant, key, recipient="dev@a.example.test"):
    rendered = render(
        "gate.waiting",
        {"run_id": "run-1", "team": "payments", "gate": "g", "role": "team-lead"},
        tenant_name="Tenant",
    )
    async with tenant_tx(api.db.app, tenant.id) as conn:
        return await enqueue(
            conn,
            kind="gate.waiting",
            recipient=recipient,
            rendered=rendered,
            dedupe_key=key,
            actor="system:control-plane",
            clock=api.clock,
        )


async def _rows(api, tenant):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        return list(await conn.execute(select(notifications)))


async def test_enqueue_is_idempotent_per_dedupe_key(api):
    first = await _queue(api, api.a, "gate:run-1:g:1")
    again = await _queue(api, api.a, "gate:run-1:g:1")
    assert first is not None and again is None
    rows = await _rows(api, api.a)
    assert [(row.status, row.attempts, row.dedupe_key) for row in rows] == [
        ("pending", 0, "gate:run-1:g:1")
    ]
    async with tenant_tx(api.db.app, api.a.id) as conn:
        queued = [r for r in await read_records(conn) if r.event == "notification.queued"]
    assert len(queued) == 1 and queued[0].target == f"notification:{first}"
    assert "body" not in str(queued[0].after)


async def test_rolled_back_event_queues_nothing(api):
    rendered = render(
        "gate.waiting",
        {"run_id": "r", "team": "payments", "gate": "g", "role": "x"},
        tenant_name="Tenant",
    )
    with pytest.raises(RuntimeError):
        async with tenant_tx(api.db.app, api.a.id) as conn:
            await enqueue(
                conn,
                kind="gate.waiting",
                recipient="dev@a.example.test",
                rendered=rendered,
                dedupe_key="k",
                actor="system:control-plane",
                clock=api.clock,
            )
            raise RuntimeError("the event failed")
    assert await _rows(api, api.a) == []


async def test_one_tenants_outbox_is_invisible_to_another(api):
    await _queue(api, api.a, "same-key")
    assert await _rows(api, api.b) == []
    assert await _queue(api, api.b, "same-key") is not None  # keys are per tenant
    async with tenant_tx(api.db.app, api.b.id) as conn:
        count = (await conn.execute(select(func.count()).select_from(notifications))).scalar()
    assert count == 1
