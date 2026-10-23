"""Stopping and holding org runs (M2-25; spec §8.6, M2-06, M2-10):

    agentic killswitch on <scope> --reason TEXT [--team T]    stop matching runs now
    agentic killswitch off <id> [--reason TEXT]               clear a switch
    agentic killswitch list [--all]                           the switches you may clear
    agentic pause <scope> --reason TEXT [--team T]            hold matching runs
    agentic resume --scope <scope> [--team T]                 clear that scope's pauses

A scope is `global` (the whole tenant), or `team:<slug>`, `user:<email>`,
`repo:<host/owner/name or a remote URL>`, `pipeline:<name>`, `agent:<name>[@version]`,
`model:<id>` or `backend:<name>`. `--team` limits a switch of any kind to one team's runs;
team leads must give their own team (and a `team:` scope is their team already). Org
admins may set any switch. The control plane decides who may: a refusal exits 2 with its
message. A killed run stays killed; a held run continues with `agentic resume <run>` once
its pause is cleared. Needs org mode with a control plane (`agentic login --for
control-plane`)."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from agentic.killswitch import parse_scope
from agentic.orgapi import KillSwitchInfo
from agentic_cli import control_plane
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output, table

Reason = Annotated[str, typer.Option("--reason", help="Why: recorded in the audit log.")]
TeamOption = Annotated[
    str | None, typer.Option("--team", help="Limit the switch to this team's runs.")
]


def register(app: typer.Typer) -> None:
    group = typer.Typer(
        help="Stop org runs at once (org admins; team leads in their team).", no_args_is_help=True
    )
    app.add_typer(group, name="killswitch")

    @group.command("on")
    def on(ctx: typer.Context, scope: str, reason: Reason, team: TeamOption = None) -> None:
        """Stop the runs a scope names: running steps are cancelled, runs marked killed."""
        _set(ctx.obj, scope, reason, team, "kill")

    @group.command("off")
    def off(
        ctx: typer.Context,
        switch_id: Annotated[str, typer.Argument(metavar="ID", help="From `killswitch list`.")],
        reason: Annotated[str | None, typer.Option("--reason", help="Why it is cleared.")] = None,
    ) -> None:
        """Clear a switch (held runs continue on `agentic resume <run>`)."""
        out: Output = ctx.obj
        with _client() as client:
            info = client.clear_switch(switch_id.strip(), reason)
        out.result(_data(info), lambda c: c.print(f"cleared {_line(info)}", markup=False))

    @group.command("list")
    def list_(
        ctx: typer.Context,
        show_all: Annotated[bool, typer.Option("--all", help="Include cleared switches.")] = False,
    ) -> None:
        """The switches you may clear."""
        out: Output = ctx.obj
        with _client() as client:
            found = client.switches(include_cleared=show_all)

        def show(console) -> None:
            if not found:
                console.print("No switches.")
                return
            shown = table("ID", "SCOPE", "TEAM", "MODE", "REASON", "BY", "ACTIVE")
            for info in found:
                shown.add_row(
                    str(info.id),
                    _scope(info),
                    info.team or "-",
                    info.mode,
                    info.reason,
                    info.created_by,
                    "yes" if info.active else f"cleared by {info.cleared_by}",
                )
            console.print(shown)

        out.result({"switches": [_data(info) for info in found]}, show)

    @app.command("pause")
    def pause(ctx: typer.Context, scope: str, reason: Reason, team: TeamOption = None) -> None:
        """Hold the runs a scope names after their current turn (maintenance mode)."""
        _set(ctx.obj, scope, reason, team, "pause")


def resume_scope(out: Output, scope: str, team: str | None) -> None:
    """`agentic resume --scope`: clear every active pause switch of exactly this scope."""
    kind, value = _parsed(scope)
    limited = value if kind == "team" else team
    with _client() as client:
        wanted = [
            info
            for info in client.switches()
            if info.active
            and info.mode == "pause"
            and (info.scope_kind, info.scope_value, info.team) == (kind, value, limited)
        ]
        if not wanted:
            raise CliError(
                f"no pause switch is active for {scope}",
                ExitCode.ERROR,
                hint="`agentic killswitch list` shows the switches you may clear",
            )
        cleared = [client.clear_switch(str(info.id), "resumed") for info in wanted]
    data = {"cleared": [str(info.id) for info in cleared]}

    def show(console) -> None:
        for info in cleared:
            console.print(f"cleared {_line(info)}", markup=False)
        console.print("held runs continue with `agentic resume <run>`", markup=False)

    out.result(data, show)


def _set(out: Output, scope: str, reason: str, team: str | None, mode: str) -> None:
    kind, value = _parsed(scope)
    body: dict = {"scope_kind": kind, "scope_value": value, "mode": mode, "reason": reason}
    if team:
        body["team"] = team.strip()
    with _client() as client:
        info = client.set_switch(body)
    out.result(_data(info), lambda c: c.print(f"set {_line(info)}", markup=False))


def _parsed(scope: str) -> tuple[str, str]:
    try:
        return parse_scope(scope)
    except ValueError as error:
        raise CliError(str(error), ExitCode.USAGE) from None


def _client() -> control_plane.ControlPlaneClient:
    home = agentic_home()
    settings = user_config(home)
    client = control_plane.client_for(os.environ, settings, home) if settings.get("org") else None
    if client is None:
        raise CliError(
            "switches live in the org's control plane: this needs org mode with one",
            ExitCode.REFUSED,
            hint="set org and control_plane_url in ~/.agentic/config.yaml, then run "
            f"`{control_plane.LOGIN}`",
        )
    return client


def _scope(info: KillSwitchInfo) -> str:
    return "global" if info.scope_kind == "tenant" else f"{info.scope_kind}:{info.scope_value}"


def _line(info: KillSwitchInfo) -> str:
    where = f" (team {info.team})" if info.team and info.scope_kind != "team" else ""
    return f"{info.mode} switch {info.id}: {_scope(info)}{where} - {info.reason}"


def _data(info: KillSwitchInfo) -> dict:
    return info.model_dump(mode="json")
