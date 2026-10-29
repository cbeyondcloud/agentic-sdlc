"""`python -m agentic_cp <command>`: the server's operator entry point.

    serve                 run the API (uvicorn); refuses a database that does not enforce
                          tenant isolation and insert-only evidence
    worker                the background worker: policy sync, CODEOWNERS refresh, auto-trip,
                          mail and key revocations for every tenant (one leader at a time);
                          refuses the databases `serve` refuses
    migrate               upgrade the database (owner role, AGENTIC_CP_OWNER_DATABASE_URL),
                          re-align the runtime role's grants, seed every tenant's builtin
                          roles and report the database guarantees
    db grant-runtime      create or re-align the runtime role of AGENTIC_CP_DATABASE_URL
    db link-usage --host H [--port P] --dbname D --user U [--password-env VAR]
                          point the dashboards' usage views at the gateway's tables
                          (postgres_fdw; the password only from the variable)
    db dashboard-role --tenant S --role R [--password-env VAR]
                          bind one Grafana reader role to one tenant: the dash_* views,
                          that tenant's rows only
    db verify-restore     check a restored database (owner role): migration revision, the
                          runtime role's guarantees, every tenant's audit chain
    admin audit verify --tenant S
                          verify one tenant's audit chain
    admin tenant create --slug S --name N --admin-email E [--policy-repo URL]
                        [--policy-branch B] [--gateway-tenant T]
                          create a tenant, its roles and first org admin; prints the token once
    admin tenant list     list tenants
    admin tenant set-policy-repo --tenant S --repo URL [--branch B]
                          point one tenant at its git policy repo
    admin tenant set-mail-from --tenant S (--address A [--name N] | --clear)
                          one tenant's own mail sender
    admin tenant suspend|resume --tenant S --reason R
                          refuse (or again accept) one tenant's tokens and invites
    admin user enable --tenant S --email E --reason R
                          re-enable an offboarded person (then invite them again)
    admin issue-token --tenant S --email E
                          a recovery token for an existing user of one tenant
    openapi [--write | --check]
                          write or compare control-plane/openapi.json
    dashboards (--write | --check)
                          write or compare the Grafana dashboards' JSON with the catalog

Settings come from AGENTIC_CP_* variables (see `agentic_cp.settings`). This is a server
tool run by the platform team (kubectl exec, compose run), not the developers' CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from agentic_cp.settings import Settings, SettingsError

#: The committed OpenAPI snapshot (only meaningful in a source checkout).
OPENAPI_PATH = Path(__file__).resolve().parents[2] / "openapi.json"
#: The umbrella chart's copy of the dashboards (only present in a source checkout).
CHART_DASHBOARDS = (
    Path(__file__).resolve().parents[3] / "deploy" / "helm" / "agentic-platform" / "dashboards"
)
#: A DSN for building the app without a database (OpenAPI generation never connects).
_NO_DATABASE = "postgresql+asyncpg://openapi@127.0.0.1:9/none"


def openapi_text() -> str:
    """The OpenAPI document as committed: sorted keys, two-space indent, LF, final newline."""
    from agentic_cp.app import create_app

    app = create_app(Settings.from_env({"AGENTIC_CP_DATABASE_URL": _NO_DATABASE}))
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _openapi(args: argparse.Namespace) -> int:
    text = openapi_text()
    if args.write:
        OPENAPI_PATH.write_bytes(text.encode("utf-8"))
        print(f"wrote {OPENAPI_PATH}")
        return 0
    if args.check:
        current = OPENAPI_PATH.read_bytes().decode("utf-8") if OPENAPI_PATH.is_file() else ""
        if current.replace("\r\n", "\n") != text:
            print(
                "control-plane/openapi.json is stale: run "
                "`uv run python -m agentic_cp openapi --write`",
                file=sys.stderr,
            )
            return 1
        print("openapi.json is current")
        return 0
    sys.stdout.write(text)
    return 0


def _dashboard_dirs() -> list[Path]:
    """Where the dashboards' JSON is kept: the catalog folder, and in a source checkout the
    umbrella chart's copy (Helm reads only files inside the chart, M3-02)."""
    from agentic_cp.dashboards import catalog

    dirs = [catalog.GRAFANA_DIR]
    if CHART_DASHBOARDS.parent.is_dir():
        dirs.append(CHART_DASHBOARDS)
    return dirs


def _dashboards(args: argparse.Namespace) -> int:
    """Write or compare the Grafana dashboards' JSON (M2-38) with the catalog."""
    from agentic_cp.dashboards.catalog import rendered_files

    files = rendered_files()
    if args.write:
        for folder in _dashboard_dirs():
            folder.mkdir(exist_ok=True)
            for stale in folder.glob("*.json"):
                if stale.name not in files:
                    stale.unlink()
            for name, text in files.items():
                (folder / name).write_bytes(text.encode("utf-8"))
            print(f"wrote {len(files)} dashboards to {folder}")
        return 0
    problems = []
    for folder in _dashboard_dirs():
        present = {path.name for path in folder.glob("*.json")}
        stale = sorted(
            name
            for name in present | set(files)
            if name not in files
            or name not in present
            or (folder / name).read_bytes().decode("utf-8").replace("\r\n", "\n") != files[name]
        )
        if stale:
            problems.append(f"stale dashboards in {folder}: {', '.join(stale)}")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("run `uv run python -m agentic_cp dashboards --write`", file=sys.stderr)
        return 1
    print(f"{len(files)} dashboards are current")
    return 0


def _settings() -> Settings:
    return Settings.from_env(os.environ)


_PREFLIGHT_HINT = (
    "hint: fix the database (python -m agentic_cp migrate re-applies the grants); "
    "AGENTIC_CP_REQUIRE_DB_ENFORCEMENT=false is for local development only"
)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from agentic_cp.app import create_app, serve_preflight

    settings = _settings()
    refusal = asyncio.run(serve_preflight(settings))
    if refusal:
        print(f"error: refusing to serve - {refusal}", file=sys.stderr)
        print(_PREFLIGHT_HINT, file=sys.stderr)
        return 2
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


def _worker(args: argparse.Namespace) -> int:
    """Run the background worker until interrupted."""
    import logging

    from agentic_cp.app import serve_preflight, utc_now
    from agentic_cp.db import make_engine
    from agentic_cp.gitops import GitRunner
    from agentic_cp.policy_sync.worker import run_forever

    settings = _settings()
    refusal = asyncio.run(serve_preflight(settings))  # what serve refuses, the worker does too
    if refusal:
        print(f"error: refusing to run the worker - {refusal}", file=sys.stderr)
        print(_PREFLIGHT_HINT, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    async def run() -> None:
        engine = make_engine(settings)
        try:
            await run_forever(engine, GitRunner(settings), settings, utc_now)
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("worker stopped")
    return 0


def _owner_dsn(settings: Settings) -> str:
    if settings.owner_database_url is None:
        raise SettingsError("AGENTIC_CP_OWNER_DATABASE_URL is required for this command")
    return settings.owner_database_url.get_secret_value()


def _migrate(args: argparse.Namespace) -> int:
    """Upgrade, then re-align the runtime role's grants with the new tables (a later
    evidence table must never stay writable)."""
    settings = _settings()
    head = asyncio.run(_migrate_and_regrant(settings))
    print(f"database at revision {head}")
    return 0


async def _migrate_and_regrant(settings: Settings) -> str:
    from agentic_cp.app import utc_now
    from agentic_cp.db import make_owner_engine, migrate_async
    from agentic_cp.db_roles import enforcement_status, grant_runtime, runtime_role_exists
    from agentic_cp.rbac.defaults import seed_roles
    from agentic_cp.tenancy.context import tenant_tx
    from agentic_cp.tenancy.tenants import list_tenants

    head = await migrate_async(_owner_dsn(settings), settings.db_schema)
    engine = make_owner_engine(settings)
    try:
        async with engine.begin() as conn:
            present = await runtime_role_exists(conn, settings.runtime_role)
            if present:
                await grant_runtime(conn, settings.runtime_role, None, settings.db_schema)
            known = await list_tenants(conn)
        for tenant in known:  # a release's new builtin roles reach every tenant
            async with tenant_tx(engine, tenant.id) as conn:
                added = await seed_roles(conn, clock=utc_now)
            if added:
                print(f"tenant {tenant.slug}: seeded roles {', '.join(added)}")
        if present:
            async with engine.connect() as conn:
                status = await enforcement_status(
                    conn, settings.runtime_role, schema=settings.db_schema
                )
            if status.enforced:
                print("database guarantees: enforced (tenant isolation, insert-only evidence)")
            for reason in status.reasons:
                print(f"database guarantee missing: {reason}")
        else:
            print(f"runtime role {settings.runtime_role} not found: run `db grant-runtime`")
    finally:
        await engine.dispose()
    return head


def _grant_runtime(args: argparse.Namespace) -> int:
    """Create or re-align the runtime role named in AGENTIC_CP_DATABASE_URL, with the
    password from that DSN (sent as a SCRAM verifier)."""
    from sqlalchemy.engine import make_url

    from agentic_cp.db import make_owner_engine
    from agentic_cp.db_roles import grant_runtime

    settings = _settings()
    _owner_dsn(settings)
    password = make_url(settings.database_url.get_secret_value()).password

    async def run() -> None:
        engine = make_owner_engine(settings)
        try:
            async with engine.begin() as conn:
                await grant_runtime(conn, settings.runtime_role, password, settings.db_schema)
        finally:
            await engine.dispose()

    asyncio.run(run())
    print(f"runtime role {settings.runtime_role} granted")
    return 0


def _verify_restore(args: argparse.Namespace) -> int:
    """Check a restored database (M3-02): this release's migration revision, the runtime
    role's guarantees and every tenant's audit chain. Exit 1 when it is not usable, and when
    it has no tenant unless `--allow-empty`."""
    from agentic_cp.db import make_owner_engine
    from agentic_cp.restore import verify_restored

    settings = _settings()
    _owner_dsn(settings)

    async def run():
        engine = make_owner_engine(settings)
        try:
            return await verify_restored(
                engine, settings.db_schema, settings.runtime_role, allow_empty=args.allow_empty
            )
        finally:
            await engine.dispose()

    report = asyncio.run(run())
    for line in report.lines():
        print(line)
    return 0 if report.ok else 1


class CommandError(Exception):
    """An operator command was refused; `code` is the exit status."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _secret_from(variable: str, what: str) -> str:
    """A password from the named environment variable: never from the command line, where a
    process listing would show it."""
    value = os.environ.get(variable, "")
    if not value:
        raise CommandError(f"set {variable} to {what} (it is never read from the command line)", 3)
    return value


def _link_usage(args: argparse.Namespace) -> int:
    """Point the dashboards' usage views at the gateway's tables (M2-37, ruling L1)."""
    from pydantic import SecretStr

    from agentic_cp.app import utc_now
    from agentic_cp.dashboards.link import GatewayDb, LinkError, audit_link, link_usage
    from agentic_cp.db import make_owner_engine

    settings = _settings()
    _owner_dsn(settings)
    password = _secret_from(args.password_env, "the gateway database password")
    try:
        gateway = GatewayDb(
            host=args.host,
            port=args.port,
            dbname=args.dbname,
            user=args.user,
            password=SecretStr(password),
            remote_schema=args.remote_schema,
        )
    except ValueError as error:
        raise CommandError(str(error), 3) from None

    async def run():
        engine = make_owner_engine(settings)
        try:
            async with engine.begin() as conn:
                result = await link_usage(conn, settings.db_schema, gateway)
            await audit_link(engine, gateway, result, clock=utc_now)
            return result
        finally:
            await engine.dispose()

    try:
        result = asyncio.run(run())
    except LinkError as error:
        raise CommandError(str(error), 1) from None
    print(
        f"gateway linked: {', '.join(result.linked)} read {gateway.remote_schema} in "
        f"{gateway.dbname} on {gateway.host}:{gateway.port} (server {result.server})"
    )
    return 0


def _dashboard_role(args: argparse.Namespace) -> int:
    """Bind one Grafana reader role to one tenant (M2-37, ruling T1)."""
    from agentic_cp.app import utc_now
    from agentic_cp.dashboards.readers import (
        PasswordNeeded,
        ReaderError,
        bind_reader,
        checked_role,
    )

    settings = _settings()
    _owner_dsn(settings)
    try:
        role = checked_role(args.role)
    except ReaderError as error:
        raise CommandError(str(error), 3) from None
    password = os.environ.get(args.password_env) or None

    async def work(conn, tenant):
        await bind_reader(
            conn,
            settings.db_schema,
            role,
            password,
            runtime_role=settings.runtime_role,
            clock=utc_now,
        )

    try:
        asyncio.run(_with_tenant(settings, args.tenant, work))
    except PasswordNeeded as error:
        raise CommandError(
            f"{error}: set {args.password_env} (or name another variable with --password-env)", 2
        ) from None
    except ReaderError as error:
        raise CommandError(str(error), 2) from None
    print(f"tenant {args.tenant}: dashboard reader role {role} bound (the dash_* views only)")
    return 0


async def _with_tenant(settings: Settings, slug: str, work):
    """Run `work(conn, tenant)` as the owner role inside `slug`'s tenant context: an
    operator command never touches a tenant without setting it (Review Focus 6)."""
    from agentic_cp.db import make_owner_engine
    from agentic_cp.tenancy.context import tenant_tx
    from agentic_cp.tenancy.tenants import tenant_by_slug

    engine = make_owner_engine(settings)
    try:
        async with engine.begin() as conn:
            tenant = await tenant_by_slug(conn, slug)
        if tenant is None:
            raise CommandError(f"no tenant {slug!r}", 3)
        async with tenant_tx(engine, tenant.id) as conn:
            return await work(conn, tenant)
    finally:
        await engine.dispose()


def _audit_verify(args: argparse.Namespace) -> int:
    from agentic_cp.audit import read_records, verify

    settings = _settings()
    _owner_dsn(settings)

    async def work(conn, tenant):
        return await verify(conn), len(await read_records(conn))

    problems, total = asyncio.run(_with_tenant(settings, args.tenant, work))
    for problem in problems:
        print(problem)
    if problems:
        print(f"tenant {args.tenant}: audit chain BROKEN ({len(problems)} problems)")
        return 1
    print(f"tenant {args.tenant}: audit chain intact ({total} records)")
    return 0


def _print_token(value: str) -> None:
    print("token (shown once - store it now):")
    print(value)


def _tenant_create(args: argparse.Namespace) -> int:
    from agentic_cp.app import utc_now
    from agentic_cp.auth.bootstrap import bootstrap_tenant
    from agentic_cp.auth.users import UserError
    from agentic_cp.db import make_owner_engine
    from agentic_cp.tenancy.context import TenancyError
    from agentic_cp.tenancy.tenants import TenantExists

    settings = _settings()
    _owner_dsn(settings)

    async def run():
        engine = make_owner_engine(settings)
        try:
            return await bootstrap_tenant(
                engine,
                settings,
                slug=args.slug,
                name=args.name,
                admin_email=args.admin_email,
                policy_repo=args.policy_repo,
                policy_branch=args.policy_branch,
                gateway_tenant=args.gateway_tenant,
                clock=utc_now,
            )
        finally:
            await engine.dispose()

    try:
        result = asyncio.run(run())
    except TenantExists as error:
        raise CommandError(str(error), 2) from None
    except (TenancyError, UserError) as error:
        raise CommandError(str(error), 3) from None
    print(f"tenant {result.tenant.slug} created (id {result.tenant.id})")
    print(f"first org admin: {args.admin_email.strip().lower()}")
    _print_token(result.token)
    return 0


def _tenant_list(args: argparse.Namespace) -> int:
    from agentic_cp.db import make_owner_engine
    from agentic_cp.tenancy.tenants import list_tenants

    settings = _settings()
    _owner_dsn(settings)

    async def run():
        engine = make_owner_engine(settings)
        try:
            async with engine.begin() as conn:
                return await list_tenants(conn)
        finally:
            await engine.dispose()

    for tenant in asyncio.run(run()):
        repo = tenant.policy_repo_url or "-"
        print(f"{tenant.slug}\t{tenant.status}\t{tenant.name}\t{repo}@{tenant.policy_branch}")
    return 0


def _tenant_set_policy_repo(args: argparse.Namespace) -> int:
    """Point one tenant at its policy repo (same rules as `PUT /v1/tenant/policy-source`)."""
    from sqlalchemy import update

    from agentic.orgapi.models import checked_branch
    from agentic_cp.app import utc_now
    from agentic_cp.audit import OPERATOR, AuditEvent, append
    from agentic_cp.gitops import RepoUrlError, check_repo_url
    from agentic_cp.tables import tenants

    settings = _settings()
    _owner_dsn(settings)
    try:
        repo = check_repo_url(args.repo, allow_local=settings.allow_local_repos)
        branch = checked_branch(args.branch)
    except (RepoUrlError, ValueError) as error:
        raise CommandError(str(error), 3) from None

    async def work(conn, tenant):
        await conn.execute(
            update(tenants)
            .where(tenants.c.id == tenant.id)
            .values(policy_repo_url=repo, policy_branch=branch)
        )
        await append(
            conn,
            AuditEvent(
                event="tenant.policy_source_changed",
                actor=OPERATOR,
                actor_kind="system",
                target=f"tenant:{tenant.slug}",
                before={"repo": tenant.policy_repo_url, "branch": tenant.policy_branch},
                after={"repo": repo, "branch": branch},
            ),
            clock=utc_now,
        )

    asyncio.run(_with_tenant(settings, args.tenant, work))
    print(f"tenant {args.tenant}: policy repo set (branch {branch}); the worker syncs it next")
    return 0


def _tenant_set_mail_from(args: argparse.Namespace) -> int:
    """One tenant's own mail sender (M2-07, ruling P1); `--clear` goes back to the
    deployment's `AGENTIC_CP_SMTP_FROM`."""
    from sqlalchemy import update

    from agentic_cp.app import utc_now
    from agentic_cp.audit import OPERATOR, AuditEvent, append
    from agentic_cp.settings import checked_address
    from agentic_cp.tables import tenants

    settings = _settings()
    _owner_dsn(settings)
    if args.clear:
        address, name = None, None
    else:
        if not args.address:
            raise CommandError("give --address (or --clear)", 64)
        try:
            address = checked_address(args.address)
        except ValueError as error:
            raise CommandError(f"--address: {error}", 3) from None
        name = (args.name or "").strip() or None

    async def work(conn, tenant):
        await conn.execute(
            update(tenants)
            .where(tenants.c.id == tenant.id)
            .values(mail_from=address, mail_from_name=name)
        )
        await append(
            conn,
            AuditEvent(
                event="tenant.mail_from_changed",
                actor=OPERATOR,
                actor_kind="system",
                target=f"tenant:{tenant.slug}",
                before={"address": tenant.mail_from, "name": tenant.mail_from_name},
                after={"address": address, "name": name},
            ),
            clock=utc_now,
        )

    asyncio.run(_with_tenant(settings, args.tenant, work))
    shown = address or "the deployment's sender"
    print(f"tenant {args.tenant}: mails are sent from {shown}")
    return 0


def _tenant_status(status: str):
    """`admin tenant suspend|resume` (DF-408): a suspended tenant's tokens, webhooks and
    invites are refused like unknown ones; its data stays as it is."""

    def command(args: argparse.Namespace) -> int:
        from sqlalchemy import update

        from agentic_cp.app import utc_now
        from agentic_cp.audit import OPERATOR, AuditEvent, append
        from agentic_cp.tables import tenants

        settings = _settings()
        _owner_dsn(settings)
        event = "tenant.suspended" if status == "suspended" else "tenant.resumed"

        async def work(conn, tenant):
            if tenant.status == status:
                raise CommandError(f"tenant {tenant.slug} is already {status}", 2)
            await conn.execute(
                update(tenants).where(tenants.c.id == tenant.id).values(status=status)
            )
            await append(
                conn,
                AuditEvent(
                    event=event,
                    actor=OPERATOR,
                    actor_kind="system",
                    target=f"tenant:{tenant.slug}",
                    reason=args.reason,
                    before={"status": tenant.status},
                    after={"status": status},
                ),
                clock=utc_now,
            )

        asyncio.run(_with_tenant(settings, args.tenant, work))
        print(f"tenant {args.tenant}: {status}")
        return 0

    return command


def _user_enable(args: argparse.Namespace) -> int:
    """Re-enable an offboarded person (ruling P10): their roles count again, their old
    tokens and keys stay revoked; an admin then sends a new invite."""
    from sqlalchemy import update

    from agentic_cp.app import utc_now
    from agentic_cp.audit import OPERATOR, AuditEvent, append
    from agentic_cp.auth.users import UserError, user_by_email
    from agentic_cp.tables import users
    from agentic_cp.tenancy.tenants import bump_rbac_revision, lock_rbac

    settings = _settings()
    _owner_dsn(settings)

    async def work(conn, tenant):
        await lock_rbac(conn)
        try:
            user = await user_by_email(conn, args.email)
        except UserError as error:
            raise CommandError(str(error), 3) from None
        if user is None:
            raise CommandError(f"no user {args.email!r} in tenant {tenant.slug}", 3)
        if user.status == "active":
            raise CommandError(f"{user.email} is already active", 2)
        await conn.execute(update(users).where(users.c.id == user.id).values(status="active"))
        revision = await bump_rbac_revision(conn)
        await append(
            conn,
            AuditEvent(
                event="user.enabled",
                actor=OPERATOR,
                actor_kind="system",
                target=f"user:{user.email}",
                reason=args.reason,
                before={"status": user.status},
                after={"status": "active", "rbac_revision": revision},
            ),
            clock=utc_now,
        )
        return user.email

    email = asyncio.run(_with_tenant(settings, args.tenant, work))
    print(f"{email} is active again in tenant {args.tenant}; old tokens and keys stay revoked")
    print(
        f"next: `agentic rbac invite {email}`, and `agentic killswitch off <id>` for the "
        f"user:{email} switch set at offboarding"
    )
    return 0


def _issue_token(args: argparse.Namespace) -> int:
    """Admin recovery: a token for an existing user of one named tenant."""
    from agentic_cp.app import utc_now
    from agentic_cp.audit import OPERATOR, AuditEvent, append, format_at
    from agentic_cp.auth.tokens import issue_token, token_lifetime
    from agentic_cp.auth.users import UserError, user_by_email

    settings = _settings()
    _owner_dsn(settings)

    async def work(conn, tenant):
        try:
            user = await user_by_email(conn, args.email)
        except UserError as error:
            raise CommandError(str(error), 3) from None
        if user is None:
            raise CommandError(f"no user {args.email!r} in tenant {tenant.slug}", 3)
        expires_at = utc_now() + token_lifetime(settings, user.kind, None)
        token_id, value = await issue_token(
            conn, user_id=user.id, name="recovery", expires_at=expires_at, created_by=OPERATOR
        )
        await append(
            conn,
            AuditEvent(
                event="token.issued",
                actor=OPERATOR,
                actor_kind="system",
                target=f"token:{token_id}",
                after={"name": "recovery", "user": user.email, "expires_at": format_at(expires_at)},
            ),
            clock=utc_now,
        )
        return value

    value = asyncio.run(_with_tenant(settings, args.tenant, work))
    print(f"token issued for {args.email.strip().lower()} in tenant {args.tenant}")
    _print_token(value)
    return 0


def build_parser() -> argparse.ArgumentParser:
    # the docstring is laid out as a command table: print it as written, not reflowed
    parser = argparse.ArgumentParser(
        prog="python -m agentic_cp",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(run=_serve)

    commands.add_parser("worker", help="run the background worker").set_defaults(run=_worker)

    migrate = commands.add_parser("migrate", help="upgrade the database to this release")
    migrate.set_defaults(run=_migrate)

    db = commands.add_parser("db", help="database roles").add_subparsers(
        dest="db_command", required=True
    )
    grant = db.add_parser("grant-runtime", help="create or re-align the runtime role")
    grant.set_defaults(run=_grant_runtime)
    verify_restore = db.add_parser(
        "verify-restore",
        help="check a restored database: revision, guarantees, every audit chain",
    )
    verify_restore.add_argument(
        "--allow-empty",
        action="store_true",
        help="accept a database with no tenant (a first install); otherwise it fails",
    )
    verify_restore.set_defaults(run=_verify_restore)
    link = db.add_parser(
        "link-usage", help="read the gateway's usage through postgres_fdw (dashboards)"
    )
    link.add_argument("--host", required=True, help="the gateway database's host")
    link.add_argument("--port", type=int, default=5432)
    link.add_argument("--dbname", required=True, help="the gateway database")
    link.add_argument("--user", required=True, help="a read-only gateway database role")
    link.add_argument(
        "--password-env",
        default="GATEWAY_DB_PASSWORD",
        help="the variable holding that role's password (default GATEWAY_DB_PASSWORD)",
    )
    link.add_argument("--remote-schema", default="public", help="the gateway tables' schema")
    link.set_defaults(run=_link_usage)
    reader = db.add_parser(
        "dashboard-role", help="bind one Grafana reader role to one tenant (dashboards)"
    )
    reader.add_argument("--tenant", required=True, help="the tenant's slug")
    reader.add_argument("--role", required=True, help="the login role Grafana connects as")
    reader.add_argument(
        "--password-env",
        default="DASHBOARD_DB_PASSWORD",
        help="the variable holding its password (default DASHBOARD_DB_PASSWORD; unset keeps "
        "an existing role's password)",
    )
    reader.set_defaults(run=_dashboard_role)

    admin = commands.add_parser("admin", help="operator commands (owner role)")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True)
    audit = admin_commands.add_parser("audit", help="audit chains").add_subparsers(
        dest="audit_command", required=True
    )
    audit_verify = audit.add_parser("verify", help="verify one tenant's audit chain")
    audit_verify.add_argument("--tenant", required=True, help="the tenant's slug")
    audit_verify.set_defaults(run=_audit_verify)

    tenant = admin_commands.add_parser("tenant", help="tenants").add_subparsers(
        dest="tenant_command", required=True
    )
    create = tenant.add_parser("create", help="create a tenant and its first org admin")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--admin-email", required=True)
    create.add_argument("--policy-repo", default=None, help="URL without credentials")
    create.add_argument("--policy-branch", default="main")
    create.add_argument("--gateway-tenant", default=None, help="the TokenLean tenant (ND-16)")
    create.set_defaults(run=_tenant_create)
    tenant.add_parser("list", help="list tenants").set_defaults(run=_tenant_list)
    source = tenant.add_parser("set-policy-repo", help="point one tenant at its policy repo")
    source.add_argument("--tenant", required=True, help="the tenant's slug")
    source.add_argument("--repo", required=True, help="https:// or ssh:// URL, no credentials")
    source.add_argument("--branch", default="main")
    source.set_defaults(run=_tenant_set_policy_repo)
    mail = tenant.add_parser("set-mail-from", help="one tenant's own mail sender")
    mail.add_argument("--tenant", required=True, help="the tenant's slug")
    mail.add_argument("--address", default=None, help="the sender address")
    mail.add_argument("--name", default=None, help="the sender's display name")
    mail.add_argument("--clear", action="store_true", help="use the deployment's sender")
    mail.set_defaults(run=_tenant_set_mail_from)
    for verb, status in (("suspend", "suspended"), ("resume", "active")):
        change = tenant.add_parser(verb, help=f"{verb} one tenant (audited)")
        change.add_argument("--tenant", required=True, help="the tenant's slug")
        change.add_argument("--reason", required=True, help="why (in the audit chain)")
        change.set_defaults(run=_tenant_status(status))

    user = admin_commands.add_parser("user", help="people").add_subparsers(
        dest="user_command", required=True
    )
    enable = user.add_parser("enable", help="re-enable an offboarded person (audited)")
    enable.add_argument("--tenant", required=True, help="the tenant's slug")
    enable.add_argument("--email", required=True)
    enable.add_argument("--reason", required=True, help="why (in the audit chain)")
    enable.set_defaults(run=_user_enable)

    issue = admin_commands.add_parser("issue-token", help="a recovery token for one user")
    issue.add_argument("--tenant", required=True, help="the tenant's slug")
    issue.add_argument("--email", required=True)
    issue.set_defaults(run=_issue_token)

    openapi = commands.add_parser("openapi", help="write or check openapi.json")
    mode = openapi.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    openapi.set_defaults(run=_openapi)

    dashboards = commands.add_parser("dashboards", help="write or check the Grafana dashboards")
    dash_mode = dashboards.add_mutually_exclusive_group(required=True)
    dash_mode.add_argument("--write", action="store_true")
    dash_mode.add_argument("--check", action="store_true")
    dashboards.set_defaults(run=_dashboards)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except CommandError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
