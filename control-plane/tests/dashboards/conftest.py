"""Fixtures for the dashboard views.

`dash_db`: two seeded tenants, no gateway linked (the contract views are empty).
`gateway`: a gateway database on the same server (its own database and role, reached over
`postgres_fdw` exactly as a separate server would be), filled with `dash_seed.CALLS`.
`linked_db`: `dash_db` with that gateway linked by `link_usage`.
`readers`: tenant reader roles made for one test; `bound`: readers of a and b, connected."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

import asyncpg
import pytest
from dash_seed import GATEWAY_DDL, Seeded, fill_gateway, make_tenants, seed_framework
from pg import APP_ROLE, OWNER_ROLE, connect_admin, new_database_name, quote_ident
from pydantic import SecretStr

from agentic_cp.dashboards.link import GatewayDb, link_usage
from agentic_cp.dashboards.readers import bind_reader
from agentic_cp.tenancy.context import tenant_tx

GATEWAY_ROLE = "cp_gateway"


@pytest.fixture
async def dash_db(db):
    """The per-test database with tenants a and b and their runs, events, audit records,
    repos and kill switches."""
    a, b = await make_tenants(db.owner)
    await seed_framework(db.owner, a, b)
    return db, Seeded(a=a, b=b)


@pytest.fixture(scope="session")
def gateway_password(pg_server) -> str:
    return secrets.token_hex(16)


async def prepare_link_prerequisites(admin_dsn: str) -> tuple[str, int]:
    """The one-time DBA steps on the control plane's database (postgres_fdw, the parameter
    grant for reader roles); returns the address the server answers on, which the gateway
    link uses (a non-loopback address, so the server asks for the password)."""
    admin = await connect_admin(admin_dsn)
    try:
        await admin.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
        await admin.execute(f"GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO {OWNER_ROLE}")
        await admin.execute(f"GRANT SET ON PARAMETER app.tenant_id TO {OWNER_ROLE}")
        host = await admin.fetchval("select host(inet_server_addr())")
        port = await admin.fetchval("select inet_server_port()")
    finally:
        await admin.close()
    assert host, "the test server must be reached over TCP for the gateway link"
    return host, port


@dataclass
class Gateway:
    db: GatewayDb
    dsn: str


@pytest.fixture
async def make_gateway(db, pg_server, gateway_password):
    """`await make_gateway(ddl=..., calls=...)`: a gateway database (dropped afterwards)."""
    made: list[str] = []
    host, port = await prepare_link_prerequisites(db.admin_dsn)

    async def make(ddl=GATEWAY_DDL, calls=None) -> Gateway:
        admin = await connect_admin(pg_server.admin_dsn)
        try:
            exists = await admin.fetchval("select 1 from pg_roles where rolname = $1", GATEWAY_ROLE)
            verb = "ALTER" if exists else "CREATE"
            # test-only random password; Postgres takes no bind parameter in role DDL
            await admin.execute(f"{verb} ROLE {GATEWAY_ROLE} LOGIN PASSWORD '{gateway_password}'")
            name = new_database_name("gw")
            await admin.execute(f"CREATE DATABASE {quote_ident(name)} OWNER {GATEWAY_ROLE}")
            made.append(name)
        finally:
            await admin.close()
        dsn = pg_server.dsn(user=GATEWAY_ROLE, password=gateway_password, database=name)
        await fill_gateway(dsn, ddl, **({} if calls is None else {"calls": calls}))
        link = GatewayDb(
            host=host,
            port=port,
            dbname=name,
            user=GATEWAY_ROLE,
            password=SecretStr(gateway_password),
        )
        return Gateway(db=link, dsn=dsn)

    yield make
    admin = await connect_admin(pg_server.admin_dsn)
    try:
        for name in made:
            await admin.execute(f"DROP DATABASE IF EXISTS {quote_ident(name)} WITH (FORCE)")
    finally:
        await admin.close()


@pytest.fixture
async def linked_db(dash_db, make_gateway):
    """`dash_db` with the seeded gateway linked."""
    db, seeded = dash_db
    gateway = await make_gateway()
    async with db.owner.begin() as conn:
        await link_usage(conn, db.schema, gateway.db)
    seeded.gateway_dsn = gateway.dsn
    return db, seeded, gateway


@pytest.fixture
async def roi_db(db, make_gateway):
    """Tenants a and b with the executive cost seed (`roi_seed`) and its gateway linked."""
    from roi_seed import CALLS as ROI_CALLS
    from roi_seed import seed_roi

    a, b = await make_tenants(db.owner)
    await seed_roi(db.owner, a, b)
    gateway = await make_gateway(calls=ROI_CALLS)
    async with db.owner.begin() as conn:
        await link_usage(conn, db.schema, gateway.db)
    return db, Seeded(a=a, b=b, gateway_dsn=gateway.dsn)


@pytest.fixture
async def roi_queued_db(db, make_gateway):
    """The executive cost seed plus `roi_seed.QUEUED`: a registered run with gateway calls
    and no ingested event (review F2)."""
    from roi_seed import CALLS as ROI_CALLS
    from roi_seed import QUEUED_CALLS, seed_queued, seed_roi

    a, b = await make_tenants(db.owner)
    await seed_roi(db.owner, a, b)
    await seed_queued(db.owner, a)
    gateway = await make_gateway(calls=ROI_CALLS + QUEUED_CALLS)
    async with db.owner.begin() as conn:
        await link_usage(conn, db.schema, gateway.db)
    return db, Seeded(a=a, b=b, gateway_dsn=gateway.dsn)


def _clock():
    from dash_seed import NOW

    return NOW


class Readers:
    """Reader roles made for one test, connected as Grafana connects (no session setup)."""

    def __init__(self, db, pg_server) -> None:
        self.db, self.pg_server = db, pg_server
        self.made: list[str] = []
        self.connections: list[asyncpg.Connection] = []

    def name(self, label: str) -> str:
        role = f"dash_{label}_{uuid.uuid4().hex[:10]}"
        self.made.append(role)
        return role

    async def bind(self, tenant, role: str, password: str | None) -> None:
        async with tenant_tx(self.db.owner, tenant) as conn:
            await bind_reader(
                conn, self.db.schema, role, password, runtime_role=APP_ROLE, clock=_clock
            )

    async def connect(self, role: str, password: str) -> asyncpg.Connection:
        dsn = self.pg_server.dsn(user=role, password=password, database=self.db.name)
        conn = await asyncpg.connect(dsn)
        self.connections.append(conn)
        return conn

    async def close(self) -> None:
        for conn in self.connections:
            await conn.close()
        admin = await connect_admin(self.db.admin_dsn)
        try:
            for role in self.made:
                if await admin.fetchval("select 1 from pg_roles where rolname = $1", role):
                    await admin.execute(f"DROP OWNED BY {role}")
                    await admin.execute(f"DROP ROLE {role}")
        finally:
            await admin.close()


@pytest.fixture
async def readers(linked_db, pg_server):
    db, seeded, _ = linked_db
    made = Readers(db, pg_server)
    try:
        yield db, seeded, made
    finally:
        await made.close()


@pytest.fixture
async def roi_reader(roi_db, pg_server):
    """Tenant a's dashboard reader on the executive cost seed, connected as Grafana is."""
    db, seeded = roi_db
    made = Readers(db, pg_server)
    try:
        role, password = made.name("roi"), secrets.token_hex(12)
        await made.bind(seeded.a, role, password)
        yield await made.connect(role, password)
    finally:
        await made.close()


@pytest.fixture
async def bound(readers):
    """Readers bound to a and b, connected."""
    db, seeded, made = readers
    conns = {}
    for label, tenant in (("a", seeded.a), ("b", seeded.b)):
        role, password = made.name(label), secrets.token_hex(12)
        await made.bind(tenant, role, password)
        conns[label] = await made.connect(role, password)
    return db, seeded, made, conns
