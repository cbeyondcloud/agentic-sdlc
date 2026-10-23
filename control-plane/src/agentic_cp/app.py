"""The FastAPI application factory.

`create_app` wires settings, the database engine, the clock and the injectable hooks into
`app.state`, installs the problem handlers and mounts the routes. Nothing here reads the
environment: `__main__` builds `Settings` and passes them in."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.alerts.sinks import default_sinks
from agentic_cp.audit.api import router as audit_router
from agentic_cp.auth.api import router as auth_router
from agentic_cp.auth.service_accounts import router as service_accounts_router
from agentic_cp.body_limit import BodyLimit
from agentic_cp.db import current_revision, head_revision, make_engine
from agentic_cp.db_roles import AUDIT, TENANCY, enforcement_status
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, install_problem_handlers, log
from agentic_cp.gitops import GitRunner
from agentic_cp.ingest.api import router as runs_router
from agentic_cp.keys.api import router as keys_router
from agentic_cp.keys.gateway import gateway_keys_for
from agentic_cp.killswitch.api import router as killswitch_router
from agentic_cp.logredact import install_log_redaction
from agentic_cp.metrics import Metrics, MetricsMiddleware
from agentic_cp.notify.api import router as notify_router
from agentic_cp.policy_sync.api import router as policy_router
from agentic_cp.provisioning.api import router as provisioning_router
from agentic_cp.rbac.api import router as rbac_router
from agentic_cp.rbac.authz import EnforcerCache
from agentic_cp.rbac.import_api import router as rbac_import_router
from agentic_cp.rbac.repos_api import router as repos_router
from agentic_cp.settings import Settings
from agentic_cp.tenancy.api import router as tenant_router
from agentic_cp.tenancy.ratelimit import limiter_for

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    clock: Clock | None = None,
    git: object | None = None,
    rate_limiter: object | None = None,
    gateway_keys: object | None = None,
    alert_webhook: object | None = None,
) -> FastAPI:
    """The control-plane app. An `engine` passed in stays the caller's to dispose; one the
    app makes (lazily connecting, so health works without a database) it disposes at
    shutdown."""
    owned = engine is None
    db = engine if engine is not None else make_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned:
                await db.dispose()

    app = FastAPI(
        title="agentic control plane",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.engine = db
    app.state.clock = clock or utc_now
    app.state.git = git if git is not None else GitRunner(settings)
    app.state.rate_limiter = rate_limiter if rate_limiter is not None else limiter_for(settings)
    # the gateway's key admin API (M2-17); tests pass a fake
    app.state.gateway_keys = (
        gateway_keys if gateway_keys is not None else gateway_keys_for(settings)
    )
    # where budget alerts go (M2-16): the mail outbox and the webhook seam
    app.state.alert_sinks = default_sinks(alert_webhook)
    app.state.enforcers = EnforcerCache(settings.enforcer_cache_tenants)
    install_problem_handlers(app)
    install_log_redaction()  # the webhook token travels in the path (review F2)
    app.add_middleware(BodyLimit, max_bytes=settings.max_body_bytes)
    metrics = Metrics(settings)
    app.state.metrics = metrics
    app.add_middleware(MetricsMiddleware, metrics=metrics)  # outermost: sees every status
    app.include_router(auth_router)
    app.include_router(rbac_router)
    app.include_router(rbac_import_router)
    app.include_router(audit_router)
    app.include_router(tenant_router)
    app.include_router(policy_router)
    app.include_router(repos_router)
    app.include_router(runs_router)
    app.include_router(killswitch_router)
    app.include_router(notify_router)
    app.include_router(service_accounts_router)
    app.include_router(keys_router)
    app.include_router(provisioning_router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process answers. Never touches the database."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"], responses=PROBLEM_RESPONSES)
    async def readyz() -> Readiness:
        """Readiness: the database answers, is migrated to this release and enforces tenant
        isolation and insert-only evidence (fail closed unless relaxed for development)."""
        return await check_ready(db, settings)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Response:
        """Prometheus metrics (M2-12): only with `AGENTIC_CP_METRICS_TOKEN` as the bearer
        token; without a configured token the route does not exist (404)."""
        expected = settings.metrics_token
        if expected is None:
            raise ApiProblem(404, "not_found", "no such route or object")
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        wanted = expected.get_secret_value().encode("utf-8")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode("utf-8"), wanted
        ):
            raise ApiProblem(
                401,
                "auth.invalid_token",
                "the metrics token is not valid",
                headers={"WWW-Authenticate": "Bearer"},
            )
        await metrics.refresh(db, app.state.clock)
        body, media_type = metrics.exposition()
        return Response(body, media_type=media_type)

    return app


class Readiness(BaseModel):
    status: str
    db_enforced: bool


async def serve_preflight(settings: Settings, engine: AsyncEngine | None = None) -> str | None:
    """Why `serve` must not start (the readiness checks, ND-5), or None."""
    db = engine if engine is not None else make_engine(settings)
    try:
        await check_ready(db, settings)
    except ApiProblem as problem:
        refusal = f"{problem.code}: {problem.detail}"
        if problem.code in (AUDIT, TENANCY):  # the operator needs each reason, not a summary
            async with db.connect() as conn:
                status = await enforcement_status(conn, schema=settings.db_schema)
            refusal += "".join(f"\n  - {reason}" for reason in status.reasons)
        return refusal + (f" ({problem.hint})" if problem.hint else "")
    finally:
        if engine is None:
            await db.dispose()
    return None


async def check_ready(engine: AsyncEngine, settings: Settings) -> Readiness:
    try:
        async with engine.connect() as conn:
            revision = await current_revision(conn)
            status = await enforcement_status(conn, schema=settings.db_schema)
    except (OSError, SQLAlchemyError) as error:
        log.warning("readyz: database unavailable (%s)", type(error).__name__)
        raise ApiProblem(503, "db.unavailable", "the database is not reachable") from None
    if revision != head_revision():
        raise ApiProblem(
            503,
            "db.migrations_pending",
            "the database is not migrated to this release",
            hint="run `python -m agentic_cp migrate`",
        )
    if not status.enforced:
        for reason in status.reasons:
            log.error("readyz: database guarantee missing: %s", reason)
        if settings.require_db_enforcement:
            code = AUDIT if AUDIT in status.codes else TENANCY
            what = "tenant isolation" if code == TENANCY else "insert-only audit"
            raise ApiProblem(
                503, code, f"the database does not enforce {what}; the server log has details"
            )
    return Readiness(status="ready", db_enforced=status.enforced)
