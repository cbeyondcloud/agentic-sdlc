"""The FastAPI application factory.

`create_app` wires settings, the database engine, the clock and the injectable hooks into
`app.state`, installs the problem handlers and mounts the routes. Nothing here reads the
environment: `__main__` builds `Settings` and passes them in."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.audit.api import router as audit_router
from agentic_cp.auth.api import router as auth_router
from agentic_cp.db import current_revision, head_revision, make_engine
from agentic_cp.db_roles import AUDIT, TENANCY, enforcement_status
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, install_problem_handlers, log
from agentic_cp.gitops import GitRunner
from agentic_cp.policy_sync.api import router as policy_router
from agentic_cp.rbac.api import router as rbac_router
from agentic_cp.rbac.authz import EnforcerCache
from agentic_cp.rbac.repos_api import router as repos_router
from agentic_cp.settings import Settings
from agentic_cp.tenancy.api import router as tenant_router

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
    app.state.rate_limiter = rate_limiter
    app.state.enforcers = EnforcerCache(settings.enforcer_cache_tenants)
    install_problem_handlers(app)
    app.include_router(auth_router)
    app.include_router(rbac_router)
    app.include_router(audit_router)
    app.include_router(tenant_router)
    app.include_router(policy_router)
    app.include_router(repos_router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process answers. Never touches the database."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"], responses=PROBLEM_RESPONSES)
    async def readyz() -> Readiness:
        """Readiness: the database answers, is migrated to this release and enforces tenant
        isolation and insert-only evidence (fail closed unless relaxed for development)."""
        return await check_ready(db, settings)

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
        return f"{problem.code}: {problem.detail}" + (f" ({problem.hint})" if problem.hint else "")
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
