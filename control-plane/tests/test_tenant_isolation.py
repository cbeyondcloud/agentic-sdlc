"""Generated isolation tests: every tenant-owned table x every operation, as the runtime
role (Review Focus 6). A new table without a row factory fails
`test_every_tenant_table_has_an_isolation_factory`."""

import pytest
from asyncpg.exceptions import InsufficientPrivilegeError
from isolation import OPERATIONS, PROBE, ROW_FACTORIES, create_probe, probe_row, probe_table
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from agentic_cp.db_roles import INSERT_ONLY
from agentic_cp.tables import TENANT_TABLES, metadata
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

CASES = [(table, op) for table in (PROBE, *TENANT_TABLES) for op in OPERATIONS]


def test_every_tenant_table_has_an_isolation_factory():
    assert set(TENANT_TABLES) == set(ROW_FACTORIES)


async def test_every_table_is_tenant_owned_and_forced(db):
    """The catalogue: every table but `tenants` (and Alembic's) is tenant-owned, keyed by
    `tenant_id` first, forced under RLS with the `tenant_isolation` policy; every unique
    constraint and foreign key among them includes `tenant_id`."""
    async with db.owner.connect() as conn:
        rows = await conn.execute(
            text(
                "select c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = :s and c.relkind in ('r', 'p')"
            ),
            {"s": db.schema},
        )
        tables = {row[0]: (row[1], row[2]) for row in rows}
        assert set(tables) - {"tenants", "alembic_version"} == set(TENANT_TABLES)
        for table in TENANT_TABLES:
            assert tables[table] == (True, True), table
            policies = await conn.execute(
                text(
                    "select polname, polcmd::text, pg_get_expr(polqual, polrelid), "
                    "pg_get_expr(polwithcheck, polrelid) from pg_policy "
                    "where polrelid = (:s || '.' || :t)::regclass"
                ),
                {"s": db.schema, "t": table},
            )
            by_name = {row[0]: row[1:] for row in policies}
            command, using, check = by_name["tenant_isolation"]
            assert command == "*" and "app.tenant_id" in using and "app.tenant_id" in check
            constraints = await conn.execute(
                text(
                    "select conname, contype, "
                    "(select array_agg(a.attname order by k.ord) from unnest(conkey) "
                    "with ordinality k(attnum, ord) join pg_attribute a "
                    "on a.attrelid = conrelid and a.attnum = k.attnum) "
                    "from pg_constraint where conrelid = (:s || '.' || :t)::regclass "
                    "and contype in ('p', 'u', 'f')"
                ),
                {"s": db.schema, "t": table},
            )
            for name, kind, columns in constraints:
                if kind == "p":
                    assert columns[0] == "tenant_id", (table, name)
                elif kind == "f" and columns == ["tenant_id"]:
                    continue  # the tenant reference itself
                else:
                    assert "tenant_id" in columns, (table, name)


def _table(name):
    return probe_table if name == PROBE else metadata.tables[name]


def _refused(error: DBAPIError) -> bool:
    return isinstance(error.orig.__cause__, InsufficientPrivilegeError)


async def _make(db, table, tenant):
    """A fresh row for `table` in `tenant` (parents inserted by the owner role)."""
    factory = probe_row if table == PROBE else ROW_FACTORIES[table]
    async with tenant_tx(db.owner, tenant) as conn:
        return await factory(conn, tenant)


async def _seed(db, table, tenant):
    row = await _make(db, table, tenant)
    async with tenant_tx(db.owner, tenant) as conn:
        await conn.execute(_table(table).insert().values(row))
    return row


@pytest.mark.parametrize(("table", "operation"), CASES, ids=[f"{t}-{o}" for t, o in CASES])
async def test_tenant_isolation(table, operation, two_tenants):
    db, a, b = two_tenants
    if table == PROBE:
        await create_probe(db)
    target = _table(table)
    await _seed(db, table, a)
    await _seed(db, table, b)

    if operation == "select_other":
        async with tenant_tx(db.app, a) as conn:
            rows = (await conn.execute(select(target.c.tenant_id))).all()
        assert rows and {row[0] for row in rows} == {a}
    elif operation == "select_crafted_where":
        async with tenant_tx(db.app, a) as conn:
            crafted = select(func.count()).select_from(target).where(target.c.tenant_id == b)
            assert (await conn.execute(crafted)).scalar_one() == 0
            raw = text(f"select count(*) from {table} where tenant_id = :b or true")
            assert (await conn.execute(raw, {"b": b})).scalar_one() == 1
    elif operation == "select_no_context":
        async with db.app.begin() as conn:
            assert (await conn.execute(select(func.count()).select_from(target))).scalar_one() == 0
    elif operation == "insert_other_tenant_id":
        row = await _make(db, table, b)
        with pytest.raises(DBAPIError) as caught:
            async with tenant_tx(db.app, a) as conn:
                await conn.execute(target.insert().values(row))
        assert _refused(caught.value)
    elif operation == "insert_no_context":
        row = await _make(db, table, a)
        with pytest.raises(DBAPIError) as caught:
            async with db.app.begin() as conn:
                await conn.execute(target.insert().values(row))
        assert _refused(caught.value)
    elif operation in ("update_other", "delete_other"):
        if operation == "update_other":
            statement = target.update().values(tenant_id=target.c.tenant_id)
        else:
            statement = target.delete()
        statement = statement.where(target.c.tenant_id == b)
        if table in INSERT_ONLY:
            with pytest.raises(DBAPIError) as caught:
                async with tenant_tx(db.app, a) as conn:
                    await conn.execute(statement)
            assert _refused(caught.value)
        else:
            async with tenant_tx(db.app, a) as conn:
                assert (await conn.execute(statement)).rowcount == 0
        async with tenant_tx(db.owner, b) as conn:
            count = select(func.count()).select_from(target)
            assert (await conn.execute(count)).scalar_one() == 1
    else:  # pragma: no cover - a new operation needs its case here
        raise AssertionError(f"no case for {operation}")
