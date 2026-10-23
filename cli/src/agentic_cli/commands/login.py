"""`agentic login`: store the user's gateway key in the OS keyring (spec §24 step 3, M1-53),
or, with `--for github` (or a host from `git_hosts`), the hosting token that opens pull
requests (M1-58), or, with `--for control-plane`, the org's control-plane token (SP15).

The key goes through `agentic.auth.Credentials.store` into the Windows Credential
Manager, the macOS Keychain or the Secret Service; every later command finds it there
(the `AGENTIC_TOKENLEAN_API_KEY` / `AGENTIC_GITHUB_TOKEN` / `AGENTIC_CONTROL_PLANE_TOKEN`
environment variable still wins). The key is read with no echo, or from standard input
with `--key-stdin` for scripts, and never printed. In CI there is no keyring to use, so
login is refused with what to do instead.

A control-plane token is verified first (`GET /v1/me`) and stored only when the control
plane accepts it; the URL, tenant and user it was verified for are recorded in
`~/.agentic/control-plane.json` (no secret), and the token is only ever sent to that URL.
The URL comes from `control_plane_url` in the user config or `AGENTIC_CONTROL_PLANE_URL`;
`--url` overrides it for this command and is not written to the config (ND-13).
Verifying the gateway key with the gateway comes with gateway key introspection (M2-17);
until then login says that key was stored, not verified.
"""

import os
import sys

import typer

from agentic.auth import TOKENLEAN_KEY, CredentialError, Credentials, env_var
from agentic.orgapi import Me
from agentic_cli import control_plane
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import login_names
from agentic_cli.output import Output

GATEWAY = "gateway"
CONTROL_PLANE = "control-plane"


def register(app: typer.Typer) -> None:
    @app.command("login")
    def login(
        ctx: typer.Context,
        key_stdin: bool = typer.Option(
            False, "--key-stdin", help="Read the key from standard input instead of a prompt."
        ),
        target: str = typer.Option(
            GATEWAY,
            "--for",
            help="What the key is for: gateway (default), control-plane, a git host kind "
            "such as github, or a host from git_hosts.",
        ),
        url: str | None = typer.Option(
            None, "--url", help="With --for control-plane: the control plane's URL (this time)."
        ),
    ) -> None:
        """Store your gateway key (or a control-plane or git host token) in the OS keyring."""
        out: Output = ctx.obj
        home = agentic_home()
        wanted = target.strip().lower()
        if url is not None and wanted != CONTROL_PLANE:
            raise CliError("--url is for --for control-plane", ExitCode.USAGE)
        if wanted == CONTROL_PLANE:
            name, label = control_plane.CONTROL_PLANE_TOKEN, "Control-plane token"
            base = url or control_plane.control_plane_url(os.environ, user_config(home))
            if not base:
                raise CliError(
                    "no control plane to sign in to",
                    ExitCode.INVALID,
                    hint="set control_plane_url in ~/.agentic/config.yaml or "
                    f"{control_plane.URL_ENV} (your invite names it), or pass --url",
                )
            base = control_plane.checked_base(base)  # before the token is asked for (DF-498)
        else:
            name, label = _credential(target)
        credentials = Credentials()
        if credentials.in_ci:
            raise CliError(
                f"login stores keys in the OS keyring, which is not used in CI "
                f"({credentials.ci_marker} is set)",
                ExitCode.REFUSED,
                hint=f"set {env_var(name)} from the CI system's secrets instead",
            )
        value = _read(key_stdin, label)
        if wanted == CONTROL_PLANE:
            with control_plane.ControlPlaneClient(
                base, value, transport=control_plane.make_transport()
            ) as client:
                try:
                    me = client.me()  # verified before anything is stored (DF-182)
                except control_plane.ControlPlaneRefused as error:
                    if error.status != 401:
                        raise
                    raise control_plane.ControlPlaneRefused(
                        str(error),
                        hint="the control plane did not accept this token: paste the whole "
                        "token from your invite, or ask your org admin for a new one",
                        code_name=error.code_name,
                        status=error.status,
                    ) from None
                base = client.base_url
        try:
            credentials.store(name, value)
        except CredentialError as error:
            raise CliError(str(error), ExitCode.ERROR, hint=f"or set {env_var(name)}") from error
        if wanted == CONTROL_PLANE:
            control_plane.record_login(home, base, me.tenant.slug, me.user.email)
            _signed_in(out, base, me)
            return
        data = {"stored": name, "where": "keyring", "verified": False}
        if name == TOKENLEAN_KEY:
            shown = (
                "Key stored in the OS keyring (not yet verified with the gateway). "
                "Next: cd <repo> and run `agentic init`."
            )
        else:
            shown = (
                f"Token stored in the OS keyring as {name} (not yet verified). "
                "`agentic pr open` and `agentic init --repo` use it."
            )
        out.result(data, lambda c: c.print(shown, markup=False))


def _read(key_stdin: bool, label: str) -> str:
    if key_stdin:
        value = sys.stdin.readline().strip()
    else:
        value = typer.prompt(label, hide_input=True).strip()
    if not value:
        raise CliError("no key given", ExitCode.INVALID, hint="paste the key from your invite")
    return value


def _signed_in(out: Output, url: str, me: Me) -> None:
    org_roles = sorted(a.role for a in me.assignments if a.scope == "org")
    teams: dict[str, list[str]] = {}
    for held in me.assignments:
        if held.scope == "team" and held.team:
            teams.setdefault(held.team, []).append(held.role)
    teams = {team: sorted(roles) for team, roles in sorted(teams.items())}
    data = {
        "stored": control_plane.CONTROL_PLANE_TOKEN,
        "where": "keyring",
        "verified": True,
        "url": url,
        "tenant": me.tenant.slug,
        "user": me.user.email,
        "org_roles": org_roles,
        "teams": teams,
    }
    held = [f"org: {', '.join(org_roles)}"] if org_roles else []
    held += [f"{team}: {', '.join(roles)}" for team, roles in teams.items()]
    shown = f"Signed in to {url} ({me.tenant.name}) as {me.user.email}"
    shown += f" - {'; '.join(held)}" if held else " - no roles yet (ask your team lead)"
    out.result(data, lambda c: c.print(shown, markup=False))


def _credential(target: str) -> tuple[str, str]:
    """(credential name, prompt label) for `--for <target>`."""
    wanted = target.strip().lower()
    if wanted == GATEWAY:
        return TOKENLEAN_KEY, "Gateway key"
    names = login_names(user_config(agentic_home()))
    if wanted not in names:
        choices = ", ".join([GATEWAY, CONTROL_PLANE, *sorted(names)])
        raise CliError(
            f"--for {target!r} is not something agentic keeps a key for",
            ExitCode.USAGE,
            hint=f"choose one of: {choices}",
        )
    return names[wanted], f"Token for {wanted}"
