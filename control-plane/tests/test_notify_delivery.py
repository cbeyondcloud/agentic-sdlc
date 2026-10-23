"""Mail delivery from the outbox (M2-07, Review Focus 1), against a capture relay on
127.0.0.1: STARTTLS by default and never skipped, the tenant's sender, multipart text and
HTML, retries with backoff, permanent refusals, the last attempt, and one send per row even
with two workers at once."""

import asyncio
from datetime import timedelta

import pytest
from smtp_capture import capture_relay
from sqlalchemy import select, update

from agentic_cp.audit import read_records
from agentic_cp.notify.delivery import backoff, deliver_due
from agentic_cp.notify.outbox import enqueue
from agentic_cp.notify.smtp import SmtpNotifier
from agentic_cp.notify.templates import render
from agentic_cp.tables import notifications, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


def _notifier(relay, security="starttls"):
    return SmtpNotifier(
        host="127.0.0.1",
        port=relay.port,
        security=security,
        timeout=10,
        ssl_context=relay.client_context,
    )


def _settings(api, **values):
    return api.settings.model_copy(
        update={"smtp_from": "agentic@cp.example.test", "smtp_from_name": "agentic", **values}
    )


async def _queue(api, tenant, key="k1", recipient="dev@a.example.test"):
    rendered = render(
        "gate.waiting",
        {"run_id": "run-1", "team": "payments", "gate": "design", "role": "team-lead"},
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


async def _row(api, tenant, notification_id):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        found = select(notifications).where(notifications.c.id == notification_id)
        return (await conn.execute(found)).one()


async def _events(api, tenant, event):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        return [r for r in await read_records(conn) if r.event == event]


async def test_a_queued_mail_arrives_over_starttls_with_the_tenants_sender(api):
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants)
            .where(tenants.c.id == api.a.id)
            .values(mail_from="dev-tools@a.example.test", mail_from_name="Tenant A tools")
        )
    queued = await _queue(api, api.a)
    with capture_relay() as relay:
        report = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=_settings(api)
        )
    assert (report.sent, report.retried, report.failed) == (1, 0, 0)
    [mail] = relay.capture.received
    assert mail.tls and mail.mail_from == "dev-tools@a.example.test"
    assert mail.rcpt_tos == ["dev@a.example.test"]
    message = mail.message
    assert message["From"] == "Tenant A tools <dev-tools@a.example.test>"
    assert message["Message-ID"].startswith(f"<{queued}@")
    assert message["Auto-Submitted"] == "auto-generated"
    assert message.get_content_type() == "multipart/alternative"
    parts = [part.get_content_type() for part in message.iter_parts()]
    assert parts == ["text/plain", "text/html"]
    row = await _row(api, api.a, queued)
    assert (row.status, row.attempts, row.body_text, row.body_html) == ("sent", 1, "", "")
    assert row.sent_at == api.clock.now
    [sent] = await _events(api, api.a, "notification.sent")
    assert sent.target == f"notification:{queued}"


async def test_the_deployment_sender_is_used_without_a_tenant_sender(api):
    await _queue(api, api.a)
    with capture_relay() as relay:
        await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=_settings(api)
        )
    assert relay.capture.received[0].mail_from == "agentic@cp.example.test"


async def test_relay_without_starttls_is_never_used_in_clear(api):
    queued = await _queue(api, api.a)
    with capture_relay(starttls=False) as relay:
        report = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=_settings(api)
        )
        assert relay.capture.received == []
    assert report.retried == 1
    row = await _row(api, api.a, queued)
    assert row.status == "pending" and row.attempts == 1 and "STARTTLS" in row.last_error
    assert row.body_text  # kept for the next attempt


async def test_security_none_sends_in_clear_when_configured(api):
    await _queue(api, api.a)
    with capture_relay(starttls=False) as relay:
        report = await deliver_due(
            api.db.app, api.a.id, _notifier(relay, "none"), api.clock, settings=_settings(api)
        )
        assert report.sent == 1 and relay.capture.received[0].tls is False


def test_backoff_doubles_and_is_capped():
    assert [backoff(n) for n in (1, 2, 3, 4)] == [timedelta(minutes=m) for m in (1, 2, 4, 8)]
    assert backoff(7) == timedelta(minutes=60) and backoff(20) == timedelta(minutes=60)


async def test_transient_failure_backs_off_then_sends(api):
    queued = await _queue(api, api.a)
    settings = _settings(api)
    with capture_relay() as relay:
        relay.capture.rcpt_reply = "450 mailbox busy, try later"
        first = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=settings
        )
        assert first.retried == 1
        row = await _row(api, api.a, queued)
        assert row.next_attempt_at == api.clock.now + timedelta(minutes=1)
        relay.capture.rcpt_reply = None
        early = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=settings
        )
        assert (early.sent, early.retried) == (0, 0)  # not due yet
        api.clock.advance(timedelta(minutes=1))
        later = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=settings
        )
    assert later.sent == 1 and len(relay.capture.received) == 1
    assert (await _row(api, api.a, queued)).attempts == 2


async def test_permanent_failure_fails_at_once(api):
    queued = await _queue(api, api.a)
    with capture_relay() as relay:
        relay.capture.rcpt_reply = "550 no such user here"
        report = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=_settings(api)
        )
    assert report.failed == 1
    row = await _row(api, api.a, queued)
    assert (row.status, row.body_text, row.body_html) == ("failed", "", "")
    assert "550" in row.last_error
    [failed] = await _events(api, api.a, "notification.failed")
    assert "550" in failed.reason


async def test_last_attempt_fails(api):
    queued = await _queue(api, api.a)
    settings = _settings(api, notify_max_attempts=2)
    with capture_relay() as relay:
        relay.capture.rcpt_reply = "451 local error"
        await deliver_due(api.db.app, api.a.id, _notifier(relay), api.clock, settings=settings)
        api.clock.advance(timedelta(minutes=5))
        report = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=settings
        )
    assert report.failed == 1
    row = await _row(api, api.a, queued)
    assert (row.status, row.attempts) == ("failed", 2)


async def test_unreachable_relay_is_transient(api):
    queued = await _queue(api, api.a)
    with capture_relay() as relay:
        port = relay.port
    notifier = SmtpNotifier(host="127.0.0.1", port=port, security="starttls", timeout=2)
    report = await deliver_due(api.db.app, api.a.id, notifier, api.clock, settings=_settings(api))
    assert report.retried == 1
    assert (await _row(api, api.a, queued)).status == "pending"


class _Slow:
    """Wraps a notifier and waits inside each send, so two workers overlap."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.ids = []

    async def send(self, message) -> None:
        await asyncio.sleep(0.2)
        self.ids.append(message.id)
        await self.inner.send(message)


async def test_two_workers_send_a_row_once(api):
    queued = {await _queue(api, api.a, key=f"k{n}") for n in range(3)}
    settings = _settings(api)
    with capture_relay() as relay:
        slow = _Slow(_notifier(relay))
        reports = await asyncio.gather(
            deliver_due(api.db.app, api.a.id, slow, api.clock, settings=settings),
            deliver_due(api.db.app, api.a.id, slow, api.clock, settings=settings),
        )
    assert sorted(map(str, slow.ids)) == sorted(map(str, queued))  # each once
    assert sum(r.sent for r in reports) == 3 and len(relay.capture.received) == 3


async def test_one_tenants_worker_never_sends_anothers_mail(api):
    await _queue(api, api.a, recipient="dev@a.example.test")
    await _queue(api, api.b, recipient="dev@b.example.test")
    with capture_relay() as relay:
        await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=_settings(api)
        )
    assert [m.rcpt_tos for m in relay.capture.received] == [["dev@a.example.test"]]


async def test_the_worker_pass_delivers_each_tenants_mail(api):
    from agentic_cp.gitops import GitRunner
    from agentic_cp.policy_sync.worker import run_pass

    await _queue(api, api.a, recipient="dev@a.example.test")
    await _queue(api, api.b, recipient="dev@b.example.test")
    with capture_relay(starttls=False) as relay:
        settings = _settings(api, smtp_host="127.0.0.1", smtp_port=relay.port, smtp_security="none")
        await run_pass(api.db.app, GitRunner(settings), settings, api.clock)
    assert sorted(m.rcpt_tos[0] for m in relay.capture.received) == [
        "dev@a.example.test",
        "dev@b.example.test",
    ]


async def test_admins_list_the_outbox_without_bodies(api):
    from apiclient import member

    queued = await _queue(api, api.a)
    reply = await api.get("/v1/notifications", api.a.admin_token)
    assert reply.status_code == 200, reply.text
    [shown] = reply.json()
    assert shown["id"] == str(queued) and shown["status"] == "pending"
    assert "body_text" not in shown and "body_html" not in shown
    assert (await api.get("/v1/notifications?status=sent", api.a.admin_token)).json() == []
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    refused = await api.get("/v1/notifications", token)
    assert refused.status_code == 403 and refused.json()["code"] == "rbac.forbidden"


async def test_no_sender_anywhere_leaves_mail_queued(api):
    queued = await _queue(api, api.a)
    with capture_relay() as relay:
        report = await deliver_due(
            api.db.app, api.a.id, _notifier(relay), api.clock, settings=api.settings
        )
        assert relay.capture.received == []
    assert (report.sent, report.retried, report.failed) == (0, 0, 0)
    assert (await _row(api, api.a, queued)).attempts == 0
