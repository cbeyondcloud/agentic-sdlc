"""Alembic migrations: `upgrade head` on an empty database, idempotent, matching the table
metadata, and reversible during development."""

import asyncio

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pg import SCHEMA
from sqlalchemy import text

from agentic_cp.db import (
    downgrade_async,
    engine_for,
    head_revision,
    migrate_async,
    revision_is_known,
)
from agentic_cp.tables import metadata

pytestmark = pytest.mark.anyio


async def _catalogue(dsn: str) -> list[tuple]:
    engine = engine_for(dsn, SCHEMA)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "select table_name, column_name, data_type, is_nullable, column_default "
                    "from information_schema.columns where table_schema = :s "
                    "order by table_name, column_name"
                ),
                {"s": SCHEMA},
            )
            return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


async def test_upgrade_head_on_empty_database(blank_db):
    assert await migrate_async(blank_db, SCHEMA) == head_revision()
    tables = {row[0] for row in await _catalogue(blank_db)}
    assert {"tenants", "alembic_version"} <= tables


async def test_migrate_is_idempotent(blank_db):
    await migrate_async(blank_db, SCHEMA)
    before = await _catalogue(blank_db)
    assert await migrate_async(blank_db, SCHEMA) == head_revision()
    assert await _catalogue(blank_db) == before


async def test_migrations_match_metadata(blank_db):
    await migrate_async(blank_db, SCHEMA)
    engine = engine_for(blank_db, SCHEMA)
    try:
        async with engine.connect() as conn:
            diff = await conn.run_sync(
                lambda sync: compare_metadata(MigrationContext.configure(sync), metadata)
            )
    finally:
        await engine.dispose()
    assert diff == []


async def test_downgrade_to_base_and_upgrade_again(blank_db):
    await migrate_async(blank_db, SCHEMA)
    await downgrade_async(blank_db, SCHEMA, "base")
    tables = {row[0] for row in await _catalogue(blank_db)}
    assert tables <= {"alembic_version"}
    assert await migrate_async(blank_db, SCHEMA) == head_revision()


async def test_two_migrate_runs_at_once_both_succeed(blank_db):
    # DF-413: several pods (or a retried hook) may migrate at once; they serialise on an
    # advisory lock instead of failing with 'relation already exists'
    # (without the lock the two runs block each other: the timeout turns that into a failure)
    both = asyncio.gather(migrate_async(blank_db, SCHEMA), migrate_async(blank_db, SCHEMA))
    results = await asyncio.wait_for(both, timeout=120)
    assert results == [head_revision(), head_revision()]


def test_revision_is_known():
    assert revision_is_known(head_revision())
    assert revision_is_known("0001")
    assert not revision_is_known("m9_99_future")
