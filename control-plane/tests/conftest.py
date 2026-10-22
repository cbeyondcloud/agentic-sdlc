"""Shared fixtures for the control-plane tests.

Database fixtures (see `pg.py`): `pg_server` (session) finds or starts Postgres;
`pg_template` (session) migrates a template database once; `db` (per test) is a fresh copy
of it with an owner engine and a runtime-role engine; `blank_db` is an empty database."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from pg import (
    APP_ROLE,
    OWNER_ROLE,
    SCHEMA,
    Server,
    connect_admin,
    find_source,
    new_database_name,
    quote_ident,
    start_container,
    unavailable,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.db import engine_for, migrate_async


@pytest.fixture
def anyio_backend() -> str:
    """Async tests run on asyncio (asyncpg needs it)."""
    return "asyncio"


@dataclass(frozen=True)
class Roles:
    owner_password: str
    app_password: str


@dataclass(frozen=True)
class Template:
    name: str
    roles: Roles


@dataclass
class TestDb:
    """One test's database: DSNs for both roles and an engine for each."""

    __test__ = False  # not a test class

    name: str
    owner_dsn: str
    app_dsn: str
    owner: AsyncEngine
    app: AsyncEngine
    schema: str = SCHEMA


@pytest.fixture(scope="session")
def pg_server() -> Iterator[Server]:
    found = find_source()
    if isinstance(found, str):
        unavailable(found)
    server = Server(admin_dsn=found.dsn) if found.dsn else start_container(found.engine)
    try:
        yield server
    finally:
        server.stop()


async def _prepare_template(server: Server, template: str, roles: Roles) -> None:
    admin = await connect_admin(server.admin_dsn)
    try:
        exists = await admin.fetchval("select 1 from pg_roles where rolname = $1", OWNER_ROLE)
        verb = "ALTER" if exists else "CREATE"
        # test-only random password; Postgres takes no bind parameter in role DDL
        await admin.execute(
            f"{verb} ROLE {OWNER_ROLE} LOGIN CREATEROLE PASSWORD '{roles.owner_password}'"
        )
        await admin.execute(f"CREATE DATABASE {quote_ident(template)} OWNER {OWNER_ROLE}")
    finally:
        await admin.close()
    owner_dsn = server.dsn(user=OWNER_ROLE, password=roles.owner_password, database=template)
    await migrate_async(owner_dsn, SCHEMA)
    await after_migrate_template(owner_dsn, roles)


async def after_migrate_template(owner_dsn: str, roles: Roles) -> None:
    """What the deployment does after `migrate`: grant the runtime role (Task 3)."""


@pytest.fixture(scope="session")
def pg_template(pg_server: Server) -> Template:
    roles = Roles(owner_password=secrets.token_hex(16), app_password=secrets.token_hex(16))
    template = Template(name=new_database_name("tpl"), roles=roles)
    asyncio.run(_prepare_template(pg_server, template.name, roles))
    return template


async def _create_database(server: Server, name: str, template: str | None) -> None:
    admin = await connect_admin(server.admin_dsn)
    try:
        source = f" TEMPLATE {quote_ident(template)}" if template else ""
        await admin.execute(f"CREATE DATABASE {quote_ident(name)}{source} OWNER {OWNER_ROLE}")
    finally:
        await admin.close()


async def _drop_database(server: Server, name: str) -> None:
    admin = await connect_admin(server.admin_dsn)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {quote_ident(name)} WITH (FORCE)")
    finally:
        await admin.close()


@pytest.fixture
async def db(pg_server: Server, pg_template: Template) -> AsyncIterator[TestDb]:
    name = new_database_name()
    await _create_database(pg_server, name, pg_template.name)
    roles = pg_template.roles
    owner_dsn = pg_server.dsn(user=OWNER_ROLE, password=roles.owner_password, database=name)
    app_dsn = pg_server.dsn(user=APP_ROLE, password=roles.app_password, database=name)
    test_db = TestDb(
        name=name,
        owner_dsn=owner_dsn,
        app_dsn=app_dsn,
        owner=engine_for(owner_dsn, SCHEMA),
        app=engine_for(app_dsn, SCHEMA),
    )
    try:
        yield test_db
    finally:
        await test_db.owner.dispose()
        await test_db.app.dispose()
        await _drop_database(pg_server, name)


@pytest.fixture
async def blank_db(pg_server: Server, pg_template: Template) -> AsyncIterator[str]:
    """An empty database owned by the owner role; yields the owner's DSN."""
    name = new_database_name("blank")
    await _create_database(pg_server, name, None)
    try:
        yield pg_server.dsn(
            user=OWNER_ROLE, password=pg_template.roles.owner_password, database=name
        )
    finally:
        await _drop_database(pg_server, name)
