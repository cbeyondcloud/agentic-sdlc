"""Roles and people in the org's control plane (M2-25; M2-03, M2-14, M2-18):

    agentic rbac roles [NAME]                               the tenant's roles
    agentic rbac assignments [--team T] [--user EMAIL]      who holds what
    agentic rbac assign EMAIL ROLE [--team T]               give a role (a new person is invited)
    agentic rbac revoke EMAIL ROLE [--team T]               take it back
    agentic rbac check PERMISSION [--team T] [--repo R] [--user EMAIL]
                                                            may you (or EMAIL)? exit 0 yes, 2 no
    agentic rbac import FILE [--apply]                      roles from CSV: a dry run unless --apply
    agentic rbac service-account NAME --team T --days N     a CI account; its token is shown once
    agentic rbac invite EMAIL                               send a new invite
    agentic rbac offboard EMAIL --reason TEXT               end a person's access

The control plane decides who may: org admins anything, team leads their own team (and only
team-assignable roles). A refusal exits 2 with the control plane's message; a malformed
argument exits 64; a CSV file with bad rows exits 3, naming each row's line. The CSV file
is UTF-8 (with or without a byte order mark, LF or CRLF line ends - as Excel saves it) with
the header `email,role,team,handle`. Needs org mode with a control plane (`agentic login
--for control-plane`)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated

import typer

from agentic.orgapi import RoleImportResult
from agentic.orgapi.models import ROLE_NAME, SERVICE_NAME, TEAM_SLUG
from agentic.orgapi.rbac import PERMISSIONS
from agentic_cli import control_plane
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output, table

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

TeamOption = Annotated[str | None, typer.Option("--team", help="Within this team.")]


def register(app: typer.Typer) -> None:
    group = typer.Typer(
        help="Roles and people in the org's control plane (admins; team leads in their team).",
        no_args_is_help=True,
    )
    app.add_typer(group, name="rbac")

    @group.command("roles")
    def roles(
        ctx: typer.Context,
        name: Annotated[str | None, typer.Argument(help="One role, with its permissions.")] = None,
    ) -> None:
        """The tenant's roles."""
        out: Output = ctx.obj
        with _client() as client:
            if name is not None:
                info = client.role(_role(name))
                out.result(info.model_dump(mode="json"), _show_role(info))
                return
            found = client.roles()

        def show(console) -> None:
            shown = table("ROLE", "BUILTIN", "TEAM-ASSIGNABLE", "PERMISSIONS")
            for info in found:
                shown.add_row(
                    info.name,
                    "yes" if info.builtin else "no",
                    "yes" if info.team_assignable else "no",
                    str(len(info.permissions)),
                )
            console.print(shown)

        out.result({"roles": [info.model_dump(mode="json") for info in found]}, show)

    @group.command("assignments")
    def assignments(
        ctx: typer.Context,
        team: TeamOption = None,
        user: Annotated[str | None, typer.Option("--user", help="This person only.")] = None,
    ) -> None:
        """Who holds which role (in the org, or in one team)."""
        out: Output = ctx.obj
        with _client() as client:
            found = client.assignments(team=_team(team), user=_email(user) if user else None)

        def show(console) -> None:
            if not found:
                console.print("No assignments.")
                return
            shown = table("EMAIL", "ROLE", "SCOPE", "ID")
            for item in found:
                shown.add_row(item.email, item.role, item.team or "org", str(item.id))
            console.print(shown)

        out.result({"assignments": [item.model_dump(mode="json") for item in found]}, show)

    @group.command("assign")
    def assign(
        ctx: typer.Context,
        email: Annotated[str, typer.Argument(help="The person (created when new).")],
        role: Annotated[str, typer.Argument(help="The role, e.g. developer.")],
        team: TeamOption = None,
    ) -> None:
        """Give a person a role; a person new to the org is mailed an invite."""
        out: Output = ctx.obj
        with _client() as client:
            made = client.assign(_email(email), _role(role), _team(team))
        where = f"team {made.team}" if made.team else "the org"
        invited = " (invite mailed)" if made.invited else ""
        line = f"{made.email} holds {made.role} in {where}{invited}"
        out.result(made.model_dump(mode="json"), lambda c: c.print(line, markup=False))

    @group.command("revoke")
    def revoke(
        ctx: typer.Context,
        email: Annotated[str, typer.Argument(help="The person.")],
        role: Annotated[str, typer.Argument(help="The role to take back.")],
        team: TeamOption = None,
    ) -> None:
        """Take a role back."""
        out: Output = ctx.obj
        email, role, team = _email(email), _role(role), _team(team)
        with _client() as client:
            held = [
                item
                for item in client.assignments(team=team, user=email)
                if item.role == role and item.team == team
            ]
            if not held:
                where = f"team {team}" if team else "the org"
                raise CliError(
                    f"{email} holds no {role} assignment in {where}",
                    ExitCode.ERROR,
                    hint=f"`agentic rbac assignments --user {email}` shows what they hold",
                )
            client.delete_assignment(str(held[0].id))
        data = {"revoked": str(held[0].id), "email": email, "role": role, "team": team}
        line = f"{email} no longer holds {role}" + (f" in team {team}" if team else "")
        out.result(data, lambda c: c.print(line, markup=False))

    @group.command("check")
    def check(
        ctx: typer.Context,
        permission: Annotated[str, typer.Argument(help="e.g. gate.approve")],
        team: TeamOption = None,
        repo: Annotated[str | None, typer.Option("--repo", help="For this repo.")] = None,
        user: Annotated[
            str | None, typer.Option("--user", help="Ask about this person (needs rbac.assign).")
        ] = None,
    ) -> None:
        """May you (or --user) do PERMISSION? Exit 0 when allowed, 2 when not."""
        out: Output = ctx.obj
        if permission not in PERMISSIONS:
            raise CliError(
                f"unknown permission {permission!r}",
                ExitCode.USAGE,
                hint=f"one of: {', '.join(sorted(PERMISSIONS))}",
            )
        with _client() as client:
            result = client.check(
                permission, team=_team(team), repo=repo, user=_email(user) if user else None
            )
        verdict = "allowed" if result.allowed else "not allowed"
        line = f"{permission}: {verdict} - {result.reason}"
        out.result(result.model_dump(mode="json"), lambda c: c.print(line, markup=False))
        if not result.allowed:
            raise typer.Exit(ExitCode.REFUSED)

    @group.command("import")
    def import_(
        ctx: typer.Context,
        file: Annotated[
            Path,
            typer.Argument(exists=True, dir_okay=False, help="The CSV file (UTF-8)."),
        ],
        apply: Annotated[
            bool, typer.Option("--apply", help="Write the changes (default: a dry run).")
        ] = False,
    ) -> None:
        """Role assignments from CSV: shows what would change, writes with --apply. Only adds
        (never removes) and is safe to run again."""
        out: Output = ctx.obj
        try:
            text = file.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CliError(
                f"{file.name} is not UTF-8",
                ExitCode.INVALID,
                hint='save it as "CSV UTF-8" (Excel) or UTF-8 text',
            ) from None
        with _client() as client:
            try:
                result = client.import_roles(text, dry_run=not apply)
            except control_plane.ControlPlaneRefused as error:
                if error.code_name != "rbac.import_invalid":
                    raise
                raise CliError(
                    f"nothing was imported: {'; '.join(error.problems) or error}",
                    ExitCode.INVALID,
                    hint="run without --apply to see every row, fix the lines named, then apply",
                ) from None
        out.result(result.model_dump(mode="json"), _show_import(result))
        if result.errors:
            raise typer.Exit(ExitCode.INVALID)

    @group.command("service-account")
    def service_account(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument(help="Lower-case letters, digits and '-'.")],
        team: Annotated[str, typer.Option("--team", help="Its team.")],
        days: Annotated[
            int, typer.Option("--days", min=1, max=365, help="Token lifetime (at most 365).")
        ],
    ) -> None:
        """A team's service account for CI: it can only trigger runs. Its token is shown here
        once - put it in the CI system's secrets as AGENTIC_CONTROL_PLANE_TOKEN."""
        out: Output = ctx.obj
        if not re.fullmatch(SERVICE_NAME, name):
            raise CliError(
                "a service account's name is lower-case letters, digits and '-'", ExitCode.USAGE
            )
        with _client() as client:
            made = client.create_service_account(name, _team(team), days)

        def show(console) -> None:
            console.print(
                f"service account {made.email} (team {made.team}), token expires "
                f"{made.expires_at:%Y-%m-%d}",
                markup=False,
            )
            console.print("token (shown once - store it in your CI secrets now):", markup=False)
            console.print(made.token, markup=False)

        out.result(made.model_dump(mode="json"), show)

    @group.command("invite")
    def invite(
        ctx: typer.Context, email: Annotated[str, typer.Argument(help="The person.")]
    ) -> None:
        """Mail a person a new invite (an earlier unused one stops working)."""
        out: Output = ctx.obj
        with _client() as client:
            made = client.invite(_email(email))
        line = f"invite mailed to {made.email}; it works until {made.expires_at:%Y-%m-%d %H:%M} UTC"
        out.result(made.model_dump(mode="json"), lambda c: c.print(line, markup=False))

    @group.command("offboard")
    def offboard(
        ctx: typer.Context,
        email: Annotated[str, typer.Argument(help="The person leaving.")],
        reason: Annotated[str, typer.Option("--reason", help="Why: recorded in the audit log.")],
    ) -> None:
        """End a person's access: their tokens, invites and gateway keys are revoked and their
        running work is stopped (org admins)."""
        out: Output = ctx.obj
        with _client() as client:
            result = client.offboard(_email(email), reason)

        def show(console) -> None:
            console.print(
                f"{result.email} offboarded: {result.tokens_revoked} token(s) and "
                f"{result.invites_revoked} invite(s) revoked, gateway keys revoked: "
                f"{', '.join(result.proxy_keys_revoked) or 'none'}",
                markup=False,
            )
            if result.proxy_keys_pending:
                console.print(
                    "the gateway could not be reached for "
                    f"{', '.join(result.proxy_keys_pending)}: the control plane retries",
                    markup=False,
                )

        out.result(result.model_dump(mode="json"), show)


def _client() -> control_plane.ControlPlaneClient:
    home = agentic_home()
    settings = user_config(home)
    client = control_plane.client_for(os.environ, settings, home) if settings.get("org") else None
    if client is None:
        raise CliError(
            "roles live in the org's control plane: this needs org mode with one",
            ExitCode.REFUSED,
            hint="set org and control_plane_url in ~/.agentic/config.yaml, then run "
            f"`{control_plane.LOGIN}`",
        )
    return client


def _email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL.fullmatch(email):
        raise CliError(f"{value!r} is not an email address", ExitCode.USAGE)
    return email


def _role(value: str) -> str:
    if not re.fullmatch(ROLE_NAME, value.strip()):
        raise CliError(
            f"{value!r} is not a role name (lower-case letters, digits and '-')", ExitCode.USAGE
        )
    return value.strip()


def _team(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(TEAM_SLUG, value.strip()):
        raise CliError(
            f"{value!r} is not a team name (lower-case letters, digits and '-')", ExitCode.USAGE
        )
    return value.strip()


def _show_role(info):
    def show(console) -> None:
        console.print(
            f"{info.name} ({'builtin' if info.builtin else 'custom'}"
            f"{', team-assignable' if info.team_assignable else ''})",
            markup=False,
        )
        for permission in info.permissions:
            console.print(f"  {permission}", markup=False)

    return show


def _show_import(result: RoleImportResult):
    def show(console) -> None:
        shown = table("LINE", "EMAIL", "ROLE", "TEAM", "ACTION", "WHY")
        for row in result.rows:
            shown.add_row(
                str(row.line),
                row.email,
                row.role,
                row.team or "-",
                row.action,
                row.error or "",
            )
        console.print(shown)
        verb = "imported" if result.applied else "would import (dry run; --apply writes)"
        console.print(
            f"{verb}: {result.added} to add, {result.unchanged} unchanged, "
            f"{result.errors} with errors",
            markup=False,
        )

    return show
