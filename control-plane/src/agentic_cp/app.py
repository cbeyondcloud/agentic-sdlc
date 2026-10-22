"""The FastAPI application factory.

`create_app` wires settings, the database engine, the clock and the injectable hooks into
`app.state`, installs the problem handlers and mounts the routes. Nothing here reads the
environment: `__main__` builds `Settings` and passes them in."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.db import make_engine
from agentic_cp.errors import install_problem_handlers
from agentic_cp.settings import Settings

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
    app.state.git = git
    app.state.rate_limiter = rate_limiter
    install_problem_handlers(app)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process answers. Never touches the database."""
        return {"status": "ok"}

    return app
