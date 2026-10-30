"""Restore drill (M3-02): restore a logical backup of the control plane's schema into a fresh
Postgres and prove the result usable.

    uv run python scripts/restore_drill.py --dump agentic.dump
    uv run python scripts/restore_drill.py --dump agentic.dump --target-dsn-env SCRATCH_PG

The dump is a `pg_dump -Fc --schema=agentic` file (docs/runbooks/postgres-backup-restore.md
shows how to take one). By default the drill starts a fresh pinned Postgres container
(docker or podman; `CONTAINER_ENGINE` picks one) and runs pg_restore inside it, so nothing
but a container engine is needed on Windows or macOS. With `--target-dsn-env` it restores
into a new database on a scratch server instead (a superuser DSN, read from the named
variable; pg_restore must be on PATH), and drops that database again.

Steps: recreate the schema's owner role if the server lacks it, restore without privileges
(views left to this release's DDL, `agentic_cp.restore.filtered_toc`), re-create the views,
grant a throw-away runtime role, then `agentic_cp.restore.verify_restored`: the migration
revision, every database guarantee, every tenant's audit chain. A dump with no tenant is
not usable unless `--allow-empty` (a first install). The source database is never touched.
Exit codes: 0 usable, 1 not usable, 2 the drill itself could not run."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentic_cp.dashboards.views import view_statements
from agentic_cp.db import async_url, head_revision
from agentic_cp.db_roles import grant_runtime
from agentic_cp.restore import RestoreReport, filtered_toc, schema_owner, verify_restored
from agentic_cp.tenancy.context import install_tenant_guc

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "agentic"
#: A custom-format archive starts with these bytes.
DUMP_MAGIC = b"PGDMP"


class DrillError(Exception):
    """The drill could not run (not: the restore is unusable)."""


def check_dump(data: bytes) -> None:
    if not data.startswith(DUMP_MAGIC):
        raise DrillError(
            "the file is not a pg_dump custom-format archive: take it with `pg_dump -Fc "
            "--schema=agentic` (docs/runbooks/postgres-backup-restore.md)"
        )


def _pg_env(dsn: str, database: str) -> dict[str, str]:
    """libpq variables for `dsn` with `database`: the password never on a command line."""
    url = make_url(dsn)
    return {
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "postgres",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": database,
    }


class LocalTools:
    """pg_dump / pg_restore on PATH, against the server of `admin_dsn`."""

    def __init__(self, admin_dsn: str) -> None:
        self.admin_dsn = admin_dsn
        self._work = Path(tempfile.mkdtemp(prefix="agentic-drill-"))

    def put(self, name: str, data: bytes) -> str:
        path = self._work / name
        path.write_bytes(data)
        return str(path)

    def run(self, argv: Sequence[str], *, database: str) -> bytes:
        if shutil.which(argv[0]) is None:
            raise DrillError(f"{argv[0]} is not on PATH (use the container mode instead)")
        env = {**os.environ, **_pg_env(self.admin_dsn, database)}
        done = subprocess.run(list(argv), capture_output=True, env=env, timeout=3600)
        return _checked(argv, done)

    def close(self) -> None:
        shutil.rmtree(self._work, ignore_errors=True)


class ContainerTools:
    """pg_dump / pg_restore inside a running Postgres container, against its own server."""

    def __init__(self, engine: str, container: str, password: str | None) -> None:
        self.engine, self.container, self.password = engine, container, password or ""

    def put(self, name: str, data: bytes) -> str:
        path = f"/tmp/{name}"
        cmd = [self.engine, "exec", "-i", self.container, "sh", "-c", f"cat > {path}"]
        _checked(cmd, subprocess.run(cmd, input=data, capture_output=True, timeout=600))
        return path

    def run(self, argv: Sequence[str], *, database: str) -> bytes:
        env = {**os.environ, "PGPASSWORD": self.password}
        cmd = [
            self.engine,
            "exec",
            "-e",
            "PGPASSWORD",
            self.container,
            *argv,
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            database,
        ]
        done = subprocess.run(cmd, capture_output=True, env=env, timeout=3600)
        return _checked(argv, done)

    def close(self) -> None:
        pass


def _checked(argv: Sequence[str], done: subprocess.CompletedProcess) -> bytes:
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise DrillError(f"{argv[0]} failed ({done.returncode}): {detail}")
    return done.stdout


def _check_module():
    """scripts/check.py, loaded by path: the pinned Postgres image and the engine choice."""
    name = "_agentic_check_script"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / "check.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class FreshContainer:
    """A throw-away pinned Postgres container, published on 127.0.0.1 only."""

    def __init__(self, engine: str | None = None, *, name: str | None = None) -> None:
        self.engine = engine or _check_module().container_engine()
        if self.engine is None:
            raise DrillError("no container engine: install docker or podman, or --target-dsn-env")
        self.name = name or f"agentic-restore-drill-{os.getpid()}-{secrets.token_hex(3)}"
        self.password = secrets.token_hex(16)
        self.admin_dsn = ""

    def start(self) -> ContainerTools:
        image = _check_module().POSTGRES_IMAGE
        run = [self.engine, "run", "-d", "--rm", "--name", self.name, "-p", "127.0.0.1::5432"]
        run += ["-e", "POSTGRES_PASSWORD", image, "-c", "fsync=off"]
        env = {**os.environ, "POSTGRES_PASSWORD": self.password}
        _checked(run, subprocess.run(run, capture_output=True, env=env, timeout=600))
        port = subprocess.run(
            [self.engine, "port", self.name, "5432/tcp"], capture_output=True, text=True
        ).stdout.splitlines()
        if not port:
            raise DrillError(f"{self.name} published no port")
        number = port[0].rpartition(":")[2]
        self.admin_dsn = f"postgresql://postgres:{self.password}@127.0.0.1:{number}/postgres"
        deadline = time.monotonic() + 120
        probe = [self.engine, "exec", self.name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
        while subprocess.run(probe, capture_output=True).returncode != 0:
            if time.monotonic() > deadline:
                raise DrillError(f"{self.name} did not become ready in 120 s")
            time.sleep(0.5)
        return ContainerTools(self.engine, self.name, self.password)

    def stop(self, database: str, runtime_role: str) -> None:
        subprocess.run([self.engine, "rm", "-f", self.name], capture_output=True)


class ExistingServer:
    """A scratch server named by a superuser DSN: the drill adds one database and one role,
    and removes both again."""

    def __init__(self, admin_dsn: str) -> None:
        self.admin_dsn = admin_dsn

    def start(self) -> LocalTools:
        self.tools = LocalTools(self.admin_dsn)
        return self.tools

    def stop(self, database: str, runtime_role: str) -> None:
        self.tools.close()
        asyncio.run(_drop(self.admin_dsn, database, runtime_role))


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise DrillError(f"unsafe identifier {name!r}")
    return f'"{name}"'


async def _connect(dsn: str, timeout: float = 60.0) -> asyncpg.Connection:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await asyncpg.connect(dsn)
        except (OSError, asyncpg.CannotConnectNowError, asyncpg.PostgresConnectionError):
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.5)


async def _prepare(admin_dsn: str, database: str, owner: str) -> None:
    admin = await _connect(admin_dsn)
    try:
        if not await admin.fetchval("select 1 from pg_roles where rolname = $1", owner):
            await admin.execute(f"CREATE ROLE {_ident(owner)} NOLOGIN CREATEROLE")
        await admin.execute(f"CREATE DATABASE {_ident(database)} OWNER {_ident(owner)}")
    finally:
        await admin.close()


async def _drop(admin_dsn: str, database: str, runtime_role: str) -> None:
    admin = await _connect(admin_dsn)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {_ident(database)} WITH (FORCE)")
        await admin.execute(f"DROP ROLE IF EXISTS {_ident(runtime_role)}")
    finally:
        await admin.close()


def _database_dsn(admin_dsn: str, database: str) -> str:
    return make_url(admin_dsn).set(database=database).render_as_string(hide_password=False)


async def _finish(
    admin_dsn: str,
    database: str,
    schema: str,
    owner: str,
    runtime_role: str,
    allow_empty: bool,
) -> RestoreReport:
    """As the owner role (a superuser session with `role` set): views, runtime role, checks."""
    engine = create_async_engine(
        async_url(_database_dsn(admin_dsn, database)),
        connect_args={"server_settings": {"search_path": schema, "role": owner}},
    )
    install_tenant_guc(engine)
    try:
        async with engine.begin() as conn:
            revision = (
                await conn.exec_driver_sql("select version_num from alembic_version")
            ).scalar()
            if revision == head_revision():  # another release's views are not ours to make
                for statement in view_statements(schema):
                    await conn.exec_driver_sql(statement)
            await grant_runtime(conn, runtime_role, secrets.token_hex(16), schema)
        return await verify_restored(engine, schema, runtime_role, allow_empty=allow_empty)
    finally:
        await engine.dispose()


def run_drill(
    dump: bytes,
    target: FreshContainer | ExistingServer,
    *,
    schema: str = SCHEMA,
    owner_role: str | None = None,
    allow_empty: bool = False,
) -> RestoreReport:
    """Restore `dump` into `target` and verify it; the target is cleaned up afterwards."""
    check_dump(dump)
    suffix = secrets.token_hex(4)
    database, runtime_role = f"agentic_drill_{suffix}", f"agentic_drill_{suffix}_app"
    tools = target.start()
    try:
        dump_path = tools.put(f"agentic-drill-{suffix}.dump", dump)
        toc = tools.run(["pg_restore", "-l", dump_path], database="postgres").decode("utf-8")
        owner = owner_role or schema_owner(toc, schema)
        if owner is None:
            raise DrillError(f"the dump has no schema {schema!r} (pass --owner-role)")
        asyncio.run(_prepare(target.admin_dsn, database, owner))
        toc_path = tools.put(f"agentic-drill-{suffix}.toc", filtered_toc(toc).encode("utf-8"))
        restore = ["pg_restore", "--no-privileges", "--exit-on-error", "--single-transaction"]
        tools.run([*restore, "-L", toc_path, "-d", database, dump_path], database=database)
        return asyncio.run(
            _finish(target.admin_dsn, database, schema, owner, runtime_role, allow_empty)
        )
    finally:
        target.stop(database, runtime_role)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dump", type=Path, required=True, help="a pg_dump -Fc file")
    parser.add_argument(
        "--target-dsn-env",
        help="the variable holding a scratch server's superuser DSN (default: a fresh container)",
    )
    parser.add_argument("--schema", default=SCHEMA)
    parser.add_argument("--owner-role", help="the schema owner (default: from the dump)")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="accept a dump with no tenant (a first install); otherwise it is not usable",
    )
    args = parser.parse_args(argv)
    try:
        if args.target_dsn_env:
            dsn = os.environ.get(args.target_dsn_env, "").strip()
            if not dsn:
                raise DrillError(f"{args.target_dsn_env} is empty")
            target: FreshContainer | ExistingServer = ExistingServer(dsn)
        else:
            target = FreshContainer()
        report = run_drill(
            args.dump.read_bytes(),
            target,
            schema=args.schema,
            owner_role=args.owner_role,
            allow_empty=args.allow_empty,
        )
    except (DrillError, OSError) as error:
        print(f"restore drill could not run: {error}", file=sys.stderr)
        return 2
    for line in report.lines():
        print(line)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
