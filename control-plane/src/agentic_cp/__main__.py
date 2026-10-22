"""`python -m agentic_cp <command>`: the server's operator entry point.

    serve                 run the API (uvicorn)
    migrate               upgrade the database (owner role, AGENTIC_CP_OWNER_DATABASE_URL)
    openapi [--write | --check]
                          write or compare control-plane/openapi.json

Settings come from AGENTIC_CP_* variables (see `agentic_cp.settings`). This is a server
tool run by the platform team (kubectl exec, compose run), not the developers' CLI."""

from __future__ import annotations

import argparse
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

    from agentic_cp.app import create_app

    uvicorn.run(create_app(_settings()), host=args.host, port=args.port, log_level="info")
    return 0


def _owner_dsn(settings: Settings) -> str:
    if settings.owner_database_url is None:
        raise SettingsError("AGENTIC_CP_OWNER_DATABASE_URL is required for this command")
    return settings.owner_database_url.get_secret_value()


def _migrate(args: argparse.Namespace) -> int:
    from agentic_cp.db import migrate

    settings = _settings()
    head = migrate(_owner_dsn(settings), settings.db_schema)
    print(f"database at revision {head}")
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


if __name__ == "__main__":
    sys.exit(main())
