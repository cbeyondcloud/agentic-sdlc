"""The database engine: asyncpg through SQLAlchemy Core, `search_path` pinned to our schema.

The tenant of every transaction reaches Postgres through `tenancy.context` (a `begin`
event installed by `make_engine`), never through string-built SQL."""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentic_cp.settings import Settings


def async_url(dsn: str) -> URL:
    """`dsn` with the asyncpg driver (a plain `postgresql://` DSN is accepted)."""
    url = make_url(dsn)
    if url.drivername in ("postgresql", "postgres", "postgresql+asyncpg"):
        return url.set(drivername="postgresql+asyncpg")
    raise ValueError("the control plane needs a postgresql:// DSN")


def engine_for(dsn: str, schema: str, *, pool_size: int = 5, **options) -> AsyncEngine:
    """An engine on `dsn` whose connections resolve unqualified names in `schema` only."""
    return create_async_engine(
        async_url(dsn),
        pool_pre_ping=True,
        pool_size=pool_size,
        connect_args={"server_settings": {"search_path": schema}},
        **options,
    )


def make_engine(settings: Settings) -> AsyncEngine:
    """The runtime engine (the API's role: no ownership, no BYPASSRLS)."""
    return engine_for(settings.database_url.get_secret_value(), settings.db_schema)


def make_owner_engine(settings: Settings) -> AsyncEngine:
    """The owner engine, for migrations, grants and operator commands."""
    if settings.owner_database_url is None:
        raise ValueError("AGENTIC_CP_OWNER_DATABASE_URL is required for this command")
    return engine_for(settings.owner_database_url.get_secret_value(), settings.db_schema)
