"""The gateway link (M2-37, ruling L1): the usage contract views read the gateway's tables
through `postgres_fdw`.

`link_usage` (owner role, one transaction) makes the foreign server `<schema>_gateway` and
the owner's user mapping (created, or re-pointed), imports the gateway's `usage_events` and
`audit_events` into the schema `<schema>_gateway`, checks their columns, and re-points
`usage_source` and `gateway_audit_source` at them. Dependent views stay: the contract keeps
its columns. Re-running it is safe; pointing it at another gateway database works the same.

One mechanism for every topology: the gateway's database on the same server, in the same
database, or elsewhere. The remote session never carries our `app.tenant_id`, which the
gateway's own row-level security reads with a different meaning (its tenant name).

Prerequisites a database superuser grants once: `CREATE EXTENSION postgres_fdw` (or the
owner may create it) and `GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO <owner>`.
The gateway password is stored in the user mapping (Postgres keeps it there, readable by the
owner and superusers only) and never logged; give the link a read-only gateway role."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic_cp.dashboards.views import CONTRACTS, LINK_SUFFIX, placeholder_sql, source_sql

log = logging.getLogger("agentic_cp.dashboards.link")

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")


class LinkError(Exception):
    """The gateway could not be linked; the message never holds the password."""


def _ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def _literal(value: str) -> str:
    """`value` as a SQL string literal (option values take no bind parameters)."""
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("a control character in a connection setting")
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class GatewayDb:
    """Where the gateway's tables are, and the (read-only) role that reads them."""

    host: str
    port: int
    dbname: str
    user: str
    password: SecretStr
    remote_schema: str = "public"

    def __post_init__(self) -> None:
        if not _HOST.fullmatch(self.host):
            raise ValueError(f"--host {self.host!r} is not a host name or address")
        if not 0 < self.port < 65536:
            raise ValueError(f"--port {self.port} is not a TCP port")
        for flag, value in (("--dbname", self.dbname), ("--user", self.user)):
            if not _NAME.fullmatch(value):
                raise ValueError(f"{flag} {value!r}: letters, digits, '_', '.', '-' only")
        if not _IDENT.fullmatch(self.remote_schema):
            raise ValueError(f"--remote-schema {self.remote_schema!r} is not a schema name")
        if not self.password.get_secret_value():
            raise ValueError("the gateway password is empty")

    def with_password(self, password: str) -> GatewayDb:
        return replace(self, password=SecretStr(password))

    def described(self) -> dict[str, object]:
        """The link's settings without the password (for the audit chain and output)."""
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "remote_schema": self.remote_schema,
        }


@dataclass(frozen=True)
class LinkResult:
    """Which contract views now read the gateway, through which server and schema."""

    linked: tuple[str, ...]
    server: str
    link_schema: str


async def _execute(conn: AsyncConnection, statement: str, shown: str | None = None) -> None:
    log.info("link: %s", shown or statement)
    await conn.exec_driver_sql(statement)


async def _scalar(conn: AsyncConnection, statement: str):
    return (await conn.exec_driver_sql(statement)).scalar()


def _masked(text: str, gateway: GatewayDb) -> str:
    secret = gateway.password.get_secret_value()
    return text.replace(secret, "***") if secret else text


def _first_line(error: DBAPIError) -> str:
    return str(error.orig).strip().splitlines()[0] if error.orig else type(error).__name__


async def _ensure_wrapper(conn: AsyncConnection, link: str) -> None:
    if await _scalar(conn, "select 1 from pg_extension where extname = 'postgres_fdw'"):
        return
    try:
        await _execute(conn, f"CREATE EXTENSION postgres_fdw SCHEMA {link}")
    except DBAPIError as error:
        raise LinkError(
            f"postgres_fdw is not installed in this database ({_first_line(error)}): a "
            "superuser runs `CREATE EXTENSION postgres_fdw` and `GRANT USAGE ON FOREIGN DATA "
            "WRAPPER postgres_fdw TO <owner role>` once"
        ) from None


async def _server_and_mapping(conn: AsyncConnection, link: str, gateway: GatewayDb) -> None:
    host, port, dbname = (_literal(v) for v in (gateway.host, str(gateway.port), gateway.dbname))
    user, password = _literal(gateway.user), _literal(gateway.password.get_secret_value())
    try:
        if await _scalar(conn, f"select 1 from pg_foreign_server where srvname = '{link}'"):
            await _execute(
                conn,
                f"ALTER SERVER {link} OPTIONS (SET host {host}, SET port {port}, "
                f"SET dbname {dbname})",
            )
        else:
            await _execute(
                conn,
                f"CREATE SERVER {link} FOREIGN DATA WRAPPER postgres_fdw "
                f"OPTIONS (host {host}, port {port}, dbname {dbname})",
            )
        mapped = await _scalar(
            conn,
            f"select 1 from pg_user_mappings where srvname = '{link}' and usename = current_user",
        )
        if mapped:
            statement = (
                f"ALTER USER MAPPING FOR CURRENT_USER SERVER {link} "
                f"OPTIONS (SET user {user}, SET password {{}})"
            )
        else:
            statement = (
                f"CREATE USER MAPPING FOR CURRENT_USER SERVER {link} "
                f"OPTIONS (user {user}, password {{}})"
            )
        await _execute(conn, statement.format(password), statement.format("<password>"))
    except DBAPIError as error:
        raise LinkError(
            f"the foreign server could not be set up ({_masked(_first_line(error), gateway)}): "
            "the owner role needs USAGE on the postgres_fdw wrapper"
        ) from None


async def link_usage(conn: AsyncConnection, schema: str, gateway: GatewayDb) -> LinkResult:
    """Point the usage contract views at `gateway` (see the module docstring). Raises
    `LinkError`; the caller's transaction then rolls back and nothing changes."""
    schema = _ident(schema)
    link = _ident(schema + LINK_SUFFIX)
    remote = _ident(gateway.remote_schema)
    await _execute(conn, f"CREATE SCHEMA IF NOT EXISTS {link}")
    await _ensure_wrapper(conn, link)
    await _server_and_mapping(conn, link, gateway)
    tables = [table for _, table in CONTRACTS.values()]
    for name, (columns, _) in CONTRACTS.items():  # release the imported tables
        await _execute(conn, f"CREATE OR REPLACE VIEW {name} AS {placeholder_sql(columns)}")
    for table in tables:
        await _execute(conn, f"DROP FOREIGN TABLE IF EXISTS {link}.{table}")
    try:
        await _execute(
            conn,
            f"IMPORT FOREIGN SCHEMA {remote} LIMIT TO ({', '.join(tables)}) "
            f"FROM SERVER {link} INTO {link}",
        )
    except DBAPIError as error:
        raise LinkError(
            f"the gateway database {gateway.dbname} on {gateway.host}:{gateway.port} could "
            f"not be read ({_masked(_first_line(error), gateway)})"
        ) from None
    found = await conn.exec_driver_sql(
        "select table_name, column_name from information_schema.columns "
        f"where table_schema = '{link}'"
    )
    present: dict[str, set[str]] = {}
    for table, column in found:
        present.setdefault(table, set()).add(column)
    if "usage_events" not in present:
        raise LinkError(f"the gateway database {gateway.dbname} has no {remote}.usage_events table")
    linked = []
    for name, (columns, table) in CONTRACTS.items():
        if table not in present:
            continue  # stays empty: this gateway does not keep that table
        missing = [c for c in columns if not c.optional and c.remote not in present[table]]
        if missing:
            needs = sorted({c.requires for c in missing if c.requires})
            raise LinkError(
                f"the gateway's {table} lacks {', '.join(c.remote for c in missing)}"
                + (f": the dashboards need the gateway's {' and '.join(needs)}" if needs else "")
            )
        body = source_sql(columns, f"{link}.{table}", frozenset(present[table]))
        await _execute(conn, f"CREATE OR REPLACE VIEW {name} AS {body}")
        linked.append(name)
    return LinkResult(linked=tuple(linked), server=link, link_schema=link)


async def audit_link(
    engine: AsyncEngine,
    gateway: GatewayDb,
    result: LinkResult,
    *,
    clock: Callable[[], datetime],
) -> int:
    """Append `gateway.usage_linked` to every tenant's chain (the link changes what every
    tenant's dashboards read); returns how many chains were appended to."""
    from agentic_cp.audit import OPERATOR, AuditEvent, append
    from agentic_cp.tenancy.context import tenant_tx
    from agentic_cp.tenancy.tenants import list_tenants

    async with engine.begin() as conn:
        known = await list_tenants(conn)
    for tenant in known:
        async with tenant_tx(engine, tenant.id) as conn:
            await append(
                conn,
                AuditEvent(
                    event="gateway.usage_linked",
                    actor=OPERATOR,
                    actor_kind="system",
                    target=f"gateway:{gateway.host}/{gateway.dbname}",
                    after={**gateway.described(), "linked": list(result.linked)},
                ),
                clock=clock,
            )
    return len(known)
