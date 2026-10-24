"""Prometheus metrics (M2-12, Review Focus 6): `/metrics` is private (404 without a
configured token, 401 for a wrong one), requests are labelled by route template only,
ingest lag and results, rejections and kill-switch state reads are counted, and the
per-tenant outbox and switch gauges are read from the database at most once per interval,
with tenants beyond the cap folded into `_other`."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from apiclient import Api, member
from prometheus_client.parser import text_string_to_metric_families
from pydantic import SecretStr
from sqlalchemy import insert
from test_budget_alerts import Run

from agentic_cp.app import create_app
from agentic_cp.metrics import Metrics
from agentic_cp.settings import Settings
from agentic_cp.tables import notifications
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

TOKEN = "metrics-" + "scrape-" + "token"  # gitleaks:allow (test value)


def _samples(text: str) -> dict[tuple[str, tuple], float]:
    """Every sample as {(name, sorted labels): value}."""
    found = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            found[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return found


def _value(samples, name: str, **labels) -> float | None:
    return samples.get((name, tuple(sorted(labels.items()))))


async def _scrape(api: Api) -> dict:
    reply = await api.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert reply.status_code == 200, reply.text
    assert reply.headers["content-type"].startswith("text/plain")
    return _samples(reply.text)


@pytest.fixture
async def mapi(api):
    """The app with a metrics token configured."""
    harness = api.with_settings(metrics_token=SecretStr(TOKEN))
    try:
        yield harness
    finally:
        await harness.http.aclose()


async def test_metrics_needs_a_token(api, mapi):
    assert (await api.get("/metrics")).status_code == 404
    assert (await mapi.get("/metrics")).status_code == 401
    wrong = await mapi.get("/metrics", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert (await mapi.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})).is_success


async def test_a_blank_token_never_opens_metrics(api):
    """Review F1: settings built without `from_env` (or a token of spaces) still leave
    `/metrics` closed; `Bearer` with nothing after it never matches."""
    for blank in ("", "   "):
        harness = api.with_settings(metrics_token=SecretStr(blank))
        try:
            for header in ("Bearer", "Bearer ", "bearer  ", f"Bearer {blank}"):
                reply = await harness.get("/metrics", headers={"Authorization": header})
                assert reply.status_code == 404, (blank, header)
        finally:
            await harness.http.aclose()


def test_chain_rejection_counters_start_at_zero():
    """Review F4: a counter child made at its first increment first appears as 1, and
    `increase()` sees no rise, so the first tampering refusal after a start would not
    alert. The codes the alert names exist at 0 from the start."""
    metrics = Metrics(Settings.from_env({"AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db"}))
    samples = _samples(metrics.exposition()[0].decode("utf-8"))
    for code in ("ingest.chain_broken", "ingest.fork"):
        assert _value(samples, "agentic_cp_ingest_rejected_total", code=code) == 0, code


def test_metrics_route_is_not_in_the_openapi_document():
    app = create_app(Settings.from_env({"AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db"}))
    assert "/metrics" not in app.openapi()["paths"]


async def test_requests_are_counted_by_route_template(mapi):
    token = mapi.a.admin_token
    for run_id in ("run-one", "run-two"):
        assert (await mapi.get(f"/v1/runs/{run_id}", token)).status_code == 404
    assert (await mapi.get(f"/no/such/{uuid.uuid4()}", token)).status_code == 404
    assert (await mapi.get("/healthz")).status_code == 200
    samples = await _scrape(mapi)
    name = "agentic_cp_http_requests_total"
    assert _value(samples, name, method="GET", route="/v1/runs/{run_id}", status="4xx") == 2
    assert _value(samples, name, method="GET", route="<unmatched>", status="4xx") == 1
    assert _value(samples, name, method="GET", route="/healthz", status="2xx") == 1
    labels = " ".join(str(key) for key in samples)
    assert "run-one" not in labels and "/no/such" not in labels
    count = "agentic_cp_http_request_duration_seconds_count"
    assert _value(samples, count, method="GET", route="/v1/runs/{run_id}") == 2


async def test_ingest_lag_and_results_are_observed(mapi, made_runs):
    dev, _ = await member(mapi, mapi.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-metrics")
    run.step("1.00")
    mapi.clock.advance(timedelta(minutes=5))  # received five minutes after it happened
    await run.ingest(mapi, dev)
    await run.ingest(mapi, dev, replay=True)
    samples = await _scrape(mapi)
    assert _value(samples, "agentic_cp_ingest_events_total", result="accepted") == 2
    assert _value(samples, "agentic_cp_ingest_events_total", result="duplicate") == 2
    assert _value(samples, "agentic_cp_ingest_lag_seconds_count") == 2
    assert _value(samples, "agentic_cp_ingest_lag_seconds_sum") >= 2 * 240


async def test_rejections_count_by_code(mapi, made_runs):
    dev, _ = await member(mapi, mapi.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-gap")
    run.step("1.00")
    entries = run.ledger.events()
    reply = await mapi.post(f"/v1/runs/{run.run_id}/events", dev, {"entries": entries[1:]})
    assert reply.status_code == 409, reply.text
    samples = await _scrape(mapi)
    assert _value(samples, "agentic_cp_ingest_rejected_total", code="ingest.gap") == 1


async def test_kill_state_reads_are_counted(mapi):
    token = mapi.a.admin_token
    assert (await mapi.get("/v1/killswitch/state?team=payments", token)).status_code == 200
    made = await mapi.post(
        "/v1/killswitch", token, {"scope_kind": "tenant", "mode": "pause", "reason": "drill"}
    )
    assert made.status_code == 201, made.text
    assert (await mapi.get("/v1/killswitch/state?team=payments", token)).status_code == 200
    samples = await _scrape(mapi)
    name = "agentic_cp_killswitch_state_reads_total"
    assert _value(samples, name, result="none") == 1
    assert _value(samples, name, result="active") == 1


async def _mail(api, tenant, status: str = "pending") -> None:
    async with tenant_tx(api.db.owner, tenant.id) as conn:
        await conn.execute(
            insert(notifications).values(
                tenant_id=tenant.id,
                kind="invite",
                recipient="x@a.example.test",
                subject="s",
                body_text="t",
                body_html="h",
                dedupe_key=str(uuid.uuid4()),
                status=status,
                created_at=api.clock.now - timedelta(minutes=10),
                next_attempt_at=api.clock.now,
            )
        )


async def test_outbox_and_switch_gauges_per_tenant(mapi):
    await _mail(mapi, mapi.a)
    await _mail(mapi, mapi.a)
    await _mail(mapi, mapi.a, "failed")
    await _mail(mapi, mapi.b)
    made = await mapi.post(
        "/v1/killswitch",
        mapi.a.admin_token,
        {"scope_kind": "team", "scope_value": "payments", "mode": "pause", "reason": "drill"},
    )
    assert made.status_code == 201, made.text
    samples = await _scrape(mapi)
    assert _value(samples, "agentic_cp_outbox_pending", tenant="tenant-a") == 2
    assert _value(samples, "agentic_cp_outbox_failed", tenant="tenant-a") == 1
    assert _value(samples, "agentic_cp_outbox_pending", tenant="tenant-b") == 1
    oldest = _value(samples, "agentic_cp_outbox_oldest_pending_seconds", tenant="tenant-a")
    assert oldest == pytest.approx(600, abs=1)
    switches = "agentic_cp_killswitches_active"
    assert _value(samples, switches, tenant="tenant-a", mode="pause", source="human") == 1
    assert _value(samples, switches, tenant="tenant-b", mode="pause", source="human") is None


async def test_tenants_beyond_the_cap_fold_into_other(api):
    capped = api.with_settings(metrics_token=SecretStr(TOKEN), metrics_max_tenants=1)
    try:
        await _mail(capped, capped.a)
        await _mail(capped, capped.b)
        await _mail(capped, capped.b)
        samples = await _scrape(capped)
    finally:
        await capped.http.aclose()
    assert _value(samples, "agentic_cp_outbox_pending", tenant="tenant-a") == 1
    assert _value(samples, "agentic_cp_outbox_pending", tenant="_other") == 2
    assert _value(samples, "agentic_cp_outbox_pending", tenant="tenant-b") is None


async def test_db_gauges_refresh_at_most_every_interval(mapi):
    first = await _scrape(mapi)
    assert _value(first, "agentic_cp_outbox_pending", tenant="tenant-a") == 0
    await _mail(mapi, mapi.a)
    assert _value(await _scrape(mapi), "agentic_cp_outbox_pending", tenant="tenant-a") == 0
    mapi.clock.advance(timedelta(seconds=mapi.settings.metrics_refresh_seconds))
    assert _value(await _scrape(mapi), "agentic_cp_outbox_pending", tenant="tenant-a") == 1


async def test_streaming_export_still_streams_through_the_middleware(mapi):
    reply = await mapi.get("/v1/audit/export", mapi.a.admin_token)
    assert reply.status_code == 200
    assert reply.text.count("\n") >= 2  # header and proof at least
    samples = await _scrape(mapi)
    route = "/v1/audit/export"
    name = "agentic_cp_http_requests_total"
    assert _value(samples, name, method="GET", route=route, status="2xx") == 1


@pytest.fixture
def made_runs():
    made = []

    def make(run_id):
        made.append(Run(run_id))
        return made[-1]

    yield make
    for run in made:
        run.close()
