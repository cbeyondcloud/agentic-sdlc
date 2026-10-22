"""The database engine and migrations: asyncpg through SQLAlchemy Core, `search_path` pinned
to our schema.

The tenant of every transaction reaches Postgres through `tenancy.context` (a `begin`
event installed by `engine_for`), never through string-built SQL."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from agentic_cp.settings import SCHEMA_PATTERN, Settings


def async_url(dsn: str) -> URL:
    """`dsn` with the asyncpg driver (a plain `postgresql://` DSN is accepted)."""
    url = make_url(dsn)
    if url.drivername in ("postgresql", "postgres", "postgresql+asyncpg"):
        return url.set(drivername="postgresql+asyncpg")
    raise ValueError("the control plane needs a postgresql:// DSN")


def engine_for(dsn: str, schema: str, *, pool_size: int = 5) -> AsyncEngine:
    """An engine on `dsn` whose connections resolve unqualified names in `schema` only."""
    return create_async_engine(
        async_url(dsn),
        pool_pre_ping=True,
        pool_size=pool_size,
        connect_args={"server_settings": {"search_path": _checked_schema(schema)}},
    )


def make_engine(settings: Settings) -> AsyncEngine:
    """The runtime engine (the API's role: no ownership, no BYPASSRLS)."""
    return engine_for(settings.database_url.get_secret_value(), settings.db_schema)


def make_owner_engine(settings: Settings) -> AsyncEngine:
    """The owner engine, for migrations, grants and operator commands."""
    if settings.owner_database_url is None:
        raise ValueError("AGENTIC_CP_OWNER_DATABASE_URL is required for this command")
    return engine_for(settings.owner_database_url.get_secret_value(), settings.db_schema)


def _checked_schema(schema: str) -> str:
    if not re.fullmatch(SCHEMA_PATTERN, schema):
        raise ValueError(f"unsafe schema name {schema!r}")
    return schema


# ── migrations ────────────────────────────────────────────────────────────────

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def _alembic_config(connection=None, schema: str = "agentic") -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    config.attributes["schema"] = schema
    return config


def head_revision() -> str:
    """The newest migration shipped with this release."""
    return ScriptDirectory.from_config(_alembic_config()).get_current_head() or ""


async def current_revision(conn: AsyncConnection) -> str | None:
    """The database's migration revision, or None before the first migration."""
    found = await conn.exec_driver_sql("select to_regclass('alembic_version') is not null")
    if not found.scalar():
        return None
    return (await conn.exec_driver_sql("select version_num from alembic_version")).scalar()


async def _run_alembic(owner_dsn: str, schema: str, action, revision: str) -> str | None:
    schema = _checked_schema(schema)
    engine = engine_for(owner_dsn, schema)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        async with engine.connect() as conn:
            await conn.run_sync(lambda sync: action(_alembic_config(sync, schema), revision))
            await conn.commit()
        async with engine.connect() as conn:
            return await current_revision(conn)
    finally:
        await engine.dispose()


async def migrate_async(owner_dsn: str, schema: str) -> str:
    """`alembic upgrade head` as the owner role; returns the head revision."""
    return await _run_alembic(owner_dsn, schema, command.upgrade, "head") or ""


async def downgrade_async(owner_dsn: str, schema: str, revision: str) -> str | None:
    """`alembic downgrade <revision>` (development only)."""
    return await _run_alembic(owner_dsn, schema, command.downgrade, revision)


def migrate(owner_dsn: str, schema: str) -> str:
    """`migrate_async` in its own event loop (for `python -m agentic_cp migrate`)."""
    return asyncio.run(migrate_async(owner_dsn, schema))
