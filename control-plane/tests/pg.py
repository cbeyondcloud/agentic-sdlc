"""Test Postgres for the control plane: a server, a migrated template, a database per test.

The server is AGENTIC_CP_TEST_DSN (a superuser DSN, as `check.py --linux` provides), else a
throw-away `postgres:17` container started through CONTAINER_ENGINE (docker or podman,
never a Postgres installed on the host). Without either, database tests skip with a reason
naming both; with AGENTIC_CP_REQUIRE_PG=1 they fail instead.

Roles are cluster-wide in Postgres: `cp_owner` (owns the schema, runs migrations, CREATEROLE)
and `cp_app` (the runtime role) are made once per session; each test gets its own database
copied from a template that is migrated once."""

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import asyncpg
import pytest
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[2]
OWNER_ROLE = "cp_owner"
APP_ROLE = "cp_app"
SCHEMA = "agentic"


def _check_module():
    """scripts/check.py, loaded by path: it owns the pinned image and the engine choice."""
    name = "_agentic_check_script"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / "check.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


POSTGRES_IMAGE: str = _check_module().POSTGRES_IMAGE

NO_DATABASE = (
    "no Postgres for the control-plane database tests: set AGENTIC_CP_TEST_DSN to a superuser "
    "DSN, or install a container engine and set CONTAINER_ENGINE (docker or podman)"
)


@dataclass(frozen=True)
class Source:
    """Where the server comes from: a DSN, or a container engine to start one with."""

    dsn: str | None
    engine: str | None


def find_source(env: Mapping[str, str] = os.environ, which=shutil.which) -> Source | str:
    """The server source, or the reason there is none."""
    if dsn := env.get("AGENTIC_CP_TEST_DSN", "").strip():
        return Source(dsn=dsn, engine=None)
    try:
        engine = _check_module().container_engine(env, which)
    except SystemExit as error:  # a named engine that is unknown or missing
        return f"{error} - {NO_DATABASE}"
    return Source(dsn=None, engine=engine) if engine else NO_DATABASE


def required(env: Mapping[str, str] = os.environ) -> bool:
    return env.get("AGENTIC_CP_REQUIRE_PG", "").strip() == "1"


def unavailable(reason: str, env: Mapping[str, str] = os.environ) -> NoReturn:
    """Skip the test, or fail it when a database is required."""
    if required(env):
        pytest.fail(f"{reason} (AGENTIC_CP_REQUIRE_PG=1 makes this a failure)")
    pytest.skip(reason)


@dataclass
class Server:
    """A running Postgres server we may administer."""

    admin_dsn: str
    container: str | None = None
    engine: str | None = None

    def dsn(self, *, user: str, password: str, database: str) -> str:
        url = make_url(self.admin_dsn).set(username=user, password=password, database=database)
        return url.render_as_string(hide_password=False)

    def stop(self) -> None:
        if self.container and self.engine:
            subprocess.run([self.engine, "stop", "-t", "2", self.container], capture_output=True)


def start_container(engine: str) -> Server:
    """Start the pinned Postgres image on a random local port and wait until it answers."""
    password = secrets.token_hex(16)
    started = subprocess.run(
        [
            engine,
            "run",
            "-d",
            "--rm",
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_PASSWORD",
            POSTGRES_IMAGE,
            "-c",
            "fsync=off",
            "-c",
            "max_connections=300",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "POSTGRES_PASSWORD": password},
    )
    if started.returncode != 0:
        unavailable(f"could not start {POSTGRES_IMAGE} with {engine}: {started.stderr.strip()}")
    container = started.stdout.strip()
    server = Server(admin_dsn="", container=container, engine=engine)
    try:
        port = subprocess.run(
            [engine, "port", container, "5432/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        host, _, number = port.rpartition(":")
        server.admin_dsn = f"postgresql://postgres:{password}@127.0.0.1:{number}/postgres"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = subprocess.run(
                [engine, "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                capture_output=True,
            )
            if ready.returncode == 0:
                return server
            time.sleep(0.5)
        raise RuntimeError("the Postgres container did not become ready in 90 s")
    except BaseException:
        server.stop()
        raise


async def connect_admin(dsn: str, *, timeout: float = 60.0) -> asyncpg.Connection:
    """A connection on `dsn`, retried while the server is still starting."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await asyncpg.connect(dsn)
        except (OSError, asyncpg.CannotConnectNowError, asyncpg.PostgresConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier {name!r}")
    return f'"{name}"'


def new_database_name(prefix: str = "t") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"
