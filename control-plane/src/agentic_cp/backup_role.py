"""The logical backup's login role (M3-05, DF-891).

Every tenant table forces row-level security, on its owner too, so the owner role cannot
`pg_dump` the schema ("query would be affected by row-level security policy"). The chart's
backup CronJob logs in as a role with `BYPASSRLS` and the predefined `pg_read_all_data`
(Postgres 14+): it reads every row and can change none. Only a superuser may give a role
`BYPASSRLS`, so `create_backup_role` runs on a superuser connection (a DBA's DSN, from a
variable); `backup_role_problems` only reads `pg_roles` and runs on any connection.

The password is sent as a SCRAM verifier, never in clear, and no message repeats it."""

from __future__ import annotations

import logging
import re

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.db_roles import scram_verifier

log = logging.getLogger("agentic_cp.backup_role")

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")
#: What the backup role must never hold: it reads, it never manages anything.
_PRIVILEGED = ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication")


class BackupRoleError(Exception):
    """The backup role could not be made; nothing changed. `code` is the exit status."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def checked_role(name: str) -> str:
    """A role name we interpolate into DDL: a lower-case Postgres identifier."""
    if not _IDENT.fullmatch(name):
        raise BackupRoleError(
            f"{name!r} is not a role name: lower-case letters, digits and '_', starting with "
            "a letter or '_', at most 63",
            3,
        )
    return name


def _first_line(error: DBAPIError) -> str:
    """The driver's message alone: SQLAlchemy's text repeats the statement (the verifier)."""
    return str(error.orig).strip().splitlines()[0] if error.orig else type(error).__name__


async def _one(conn: AsyncConnection, statement: str, *params):
    return (await conn.exec_driver_sql(statement, params)).first()


async def create_backup_role(
    conn: AsyncConnection, role: str, password: str | None, *, protected: set[str]
) -> bool:
    """Create `role` (or re-align an existing one) as the backup role; True when created.
    `password=None` keeps an existing role's password. `protected` are the control plane's
    own roles, which are never turned into a backup role."""
    role = checked_role(role)
    if role in protected:
        raise BackupRoleError(f"{role} is the control plane's own role, never the backup role", 3)
    exists = await _one(conn, "select 1 from pg_roles where rolname = $1", role) is not None
    if not exists and not password:
        raise BackupRoleError(f"{role} is a new role: give it a password", 3)
    verb = "ALTER" if exists else "CREATE"
    statement = (
        f"{verb} ROLE {role} LOGIN BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
    )
    shown = statement
    if password:
        statement += f" PASSWORD '{scram_verifier(password)}'"
        shown += " PASSWORD <scram verifier>"
    try:
        log.info("backup role: %s", shown)
        await conn.exec_driver_sql(statement)
        await conn.exec_driver_sql(f"GRANT pg_read_all_data TO {role}")
    except DBAPIError as error:
        raise BackupRoleError(
            f"{role} could not be made ({_first_line(error)}): only a superuser may give a "
            f"role BYPASSRLS. Run this with a superuser DSN, or have a DBA run `CREATE ROLE "
            f"{role} LOGIN BYPASSRLS PASSWORD '...'` and `GRANT pg_read_all_data TO {role}`, "
            f"then `db backup-role --role {role} --check`",
            2,
        ) from None
    return not exists


async def backup_role_problems(conn: AsyncConnection, role: str) -> list[str]:
    """What keeps `role` from taking the backup ([] = ready)."""
    role = checked_role(role)
    row = await _one(
        conn,
        f"select rolcanlogin, rolbypassrls, {', '.join(_PRIVILEGED)}, "
        "pg_has_role(oid, 'pg_read_all_data', 'MEMBER') from pg_roles where rolname = $1",
        role,
    )
    if row is None:
        return [f"{role} does not exist: run `db backup-role --role {role}`"]
    login, bypass, *privileged, reads = row
    problems = []
    if not login:
        problems.append(f"{role} cannot log in (LOGIN)")
    if not bypass:
        problems.append(f"{role} lacks BYPASSRLS: forced row-level security hides the rows")
    if not reads:
        problems.append(f"{role} is not a member of pg_read_all_data")
    problems += [
        f"{role} holds {name.removeprefix('rol').upper()}, which a backup role must not"
        for name, held in zip(_PRIVILEGED, privileged, strict=True)
        if held
    ]
    return problems
