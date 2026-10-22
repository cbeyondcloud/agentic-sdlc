"""`python -m agentic_cp <command>`: the server's operator entry point.

    serve                 run the API (uvicorn)
    migrate               upgrade the database (owner role, AGENTIC_CP_OWNER_DATABASE_URL)
                          and re-align the runtime role's grants
    db grant-runtime      create or re-align the runtime role of AGENTIC_CP_DATABASE_URL
    admin audit verify --tenant S
                          verify one tenant's audit chain
    admin tenant create --slug S --name N --admin-email E [--policy-repo URL]
                        [--policy-branch B] [--gateway-tenant T]
                          create a tenant, its roles and first org admin; prints the token once
    admin tenant list     list tenants
    admin issue-token --tenant S --email E
                          a recovery token for an existing user of one tenant
    openapi [--write | --check]
                          write or compare control-plane/openapi.json

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


def _settings() -> Settings:
    return Settings.from_env(os.environ)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from agentic_cp.app import create_app, serve_preflight

    settings = _settings()
    refusal = asyncio.run(serve_preflight(settings))
    if refusal:
        print(f"error: refusing to serve - {refusal}", file=sys.stderr)
        return 1
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
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
    from agentic_cp.db_roles import grant_runtime, runtime_role_exists
    from agentic_cp.rbac.defaults import seed_roles
    from agentic_cp.tenancy.context import tenant_tx
    from agentic_cp.tenancy.tenants import list_tenants

    head = await migrate_async(_owner_dsn(settings), settings.db_schema)
    engine = make_owner_engine(settings)
    try:
        async with engine.begin() as conn:
            if await runtime_role_exists(conn, settings.runtime_role):
                await grant_runtime(conn, settings.runtime_role, None, settings.db_schema)
            known = await list_tenants(conn)
        for tenant in known:  # a release's new builtin roles reach every tenant
            async with tenant_tx(engine, tenant.id) as conn:
                added = await seed_roles(conn, clock=utc_now)
            if added:
                print(f"tenant {tenant.slug}: added roles {', '.join(added)}")
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


class CommandError(Exception):
    """An operator command was refused; `code` is the exit status."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


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
    parser = argparse.ArgumentParser(prog="python -m agentic_cp", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(run=_serve)

    migrate = commands.add_parser("migrate", help="upgrade the database to this release")
    migrate.set_defaults(run=_migrate)

    db = commands.add_parser("db", help="database roles").add_subparsers(
        dest="db_command", required=True
    )
    grant = db.add_parser("grant-runtime", help="create or re-align the runtime role")
    grant.set_defaults(run=_grant_runtime)

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

    issue = admin_commands.add_parser("issue-token", help="a recovery token for one user")
    issue.add_argument("--tenant", required=True, help="the tenant's slug")
    issue.add_argument("--email", required=True)
    issue.set_defaults(run=_issue_token)

    openapi = commands.add_parser("openapi", help="write or check openapi.json")
    mode = openapi.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    openapi.set_defaults(run=_openapi)
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
