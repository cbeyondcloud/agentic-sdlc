"""`agentic login`: store the user's gateway key in the OS keyring (spec §24 step 3, M1-53),
or, with `--for github` (or a host from `git_hosts`), the hosting token that opens pull
requests (M1-58), or, with `--for control-plane`, the org's control-plane token (SP15), or,
with `--for jira`, the Jira token `run --from-jira` reads issues with (M2-50; stored under
`jira.credential` when the user config names one).

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

A gateway key is checked with the configured gateway (`GET /v1/models`) before it is
stored (DF-182): a key the gateway refuses is not stored, one that cannot be checked now is
stored unverified with a warning. `agentic login --rotate` replaces the gateway key through
the control plane (M2-17); the old key stops working.
"""

import os
import sys

import httpx2
import typer

from agentic.auth import TOKENLEAN_KEY, CredentialError, Credentials, env_var
from agentic.hosting.base import scrub_url_credentials
from agentic.orgapi import Me, parse_invite
from agentic.transport import verify_gateway_key
from agentic_cli import control_plane
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import login_names
from agentic_cli.jira import JIRA, login_credential
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
            help="What the key is for: gateway (default), control-plane, jira, a git host "
            "kind such as github, or a host from git_hosts.",
        ),
        url: str | None = typer.Option(
            None, "--url", help="With --for control-plane: the control plane's URL (this time)."
        ),
        rotate: bool = typer.Option(
            False,
            "--rotate",
            help="Replace your gateway key through the control plane (the old key stops working).",
        ),
        invite: str | None = typer.Option(
            None,
            "--invite",
            help="The link from your invite mail: stores your control-plane token and gateway "
            "key (the link works once).",
        ),
    ) -> None:
        """Store your gateway key (or a control-plane or git host token) in the OS keyring."""
        out: Output = ctx.obj
        home = agentic_home()
        wanted = target.strip().lower()
        if invite is not None:
            if rotate or key_stdin or wanted != GATEWAY:
                raise CliError("--invite takes no other key option (only --url)", ExitCode.USAGE)
            _redeem(out, home, invite, url)
            return
        if url is not None and wanted != CONTROL_PLANE:
            raise CliError("--url is for --for control-plane", ExitCode.USAGE)
        if rotate:
            if wanted != GATEWAY or key_stdin:
                raise CliError(
                    "--rotate replaces the gateway key, read from no input", ExitCode.USAGE
                )
            _rotate(out, home)
            return
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
        verified = False
        if name == TOKENLEAN_KEY:
            verified = _verify_gateway_key(out, home, value)
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
        data = {"stored": name, "where": "keyring", "verified": verified}
        if name == TOKENLEAN_KEY:
            checked = "verified with the gateway" if verified else "not verified with the gateway"
            shown = (
                f"Key stored in the OS keyring ({checked}). Next: cd <repo> and run `agentic init`."
            )
        elif wanted == JIRA:
            shown = (
                f"Token stored in the OS keyring as {name} (not yet verified). "
                "`agentic run --from-jira` uses it."
            )
        else:
            shown = (
                f"Token stored in the OS keyring as {name} (not yet verified). "
                "`agentic pr open` and `agentic init --repo` use it."
            )
        out.result(data, lambda c: c.print(shown, markup=False))


def gateway_transport() -> httpx2.BaseTransport | None:
    """The transport for the gateway check (None: real calls). Tests replace it."""
    return None


def _gateway_url(home) -> str | None:
    found = os.environ.get("AGENTIC_GATEWAY_URL", "").strip() or str(
        user_config(home).get("gateway_url") or ""
    )
    return found.strip().rstrip("/") or None


def _verify_gateway_key(out: Output, home, value: str) -> bool:
    """Check the key with the configured gateway before it is stored (DF-182): a key the
    gateway refuses is not stored; one that cannot be checked now is stored unverified."""
    url = _gateway_url(home)
    if url is None:
        return False
    verdict = verify_gateway_key(url, value, transport=gateway_transport())
    if verdict == "rejected":
        raise CliError(
            f"the gateway at {scrub_url_credentials(url)} did not accept this key",
            ExitCode.REFUSED,
            hint="paste the whole key from your invite, or ask your org admin for a new "
            "invite (`agentic login --invite <link>`)",
        )
    if verdict == "unknown":
        out.warn(
            f"the key could not be checked with the gateway at {scrub_url_credentials(url)} "
            "(unreachable or an unexpected answer); it is stored unverified"
        )
        return False
    return True


def _rotate(out: Output, home) -> None:
    """`agentic login --rotate`: a new gateway key from the control plane (M2-17), stored
    in the keyring in place of the old one, which stops working."""
    credentials = Credentials()
    if credentials.in_ci:
        raise CliError(
            f"rotation stores the new key in the OS keyring, which is not used in CI "
            f"({credentials.ci_marker} is set)",
            ExitCode.REFUSED,
            hint="rotate the CI system's secret through your org admin instead",
        )
    client = control_plane.client_for(os.environ, user_config(home), home)
    if client is None:
        raise CliError(
            "rotating the gateway key goes through the org's control plane, and none is set",
            ExitCode.REFUSED,
            hint=f"set control_plane_url in ~/.agentic/config.yaml, then `{control_plane.LOGIN}`",
        )
    with client:
        issued = client.rotate_proxy_key()
    try:
        credentials.store(TOKENLEAN_KEY, issued.key)
    except CredentialError as error:
        raise CliError(
            f"the key was rotated but could not be stored: {error}",
            ExitCode.ERROR,
            hint="run `agentic login --rotate` again (the new key is not shown anywhere else)",
        ) from error
    data = {"stored": TOKENLEAN_KEY, "where": "keyring", "rotated": True, "key_id": issued.key_id}
    shown = f"Gateway key rotated (new key {issued.key_id}) and stored in the OS keyring."
    out.result(data, lambda c: c.print(shown, markup=False))


def _redeem(out: Output, home, link: str, url: str | None) -> None:
    """`agentic login --invite <link>` (M2-18): redeem the invite once and keep what it
    gives in the OS keyring. The keyring is checked before the link is used up."""
    base, token = parse_invite(link)
    base = base or url or control_plane.control_plane_url(os.environ, user_config(home))
    if not base or not token:
        raise CliError(
            "the invite names no control plane",
            ExitCode.INVALID,
            hint="paste the whole link from the invite mail, or give --url with a bare token",
        )
    base = control_plane.checked_base(base)
    credentials = Credentials()
    if credentials.in_ci:
        raise CliError(
            f"an invite stores keys in the OS keyring, which is not used in CI "
            f"({credentials.ci_marker} is set)",
            ExitCode.REFUSED,
            hint="redeem the invite on your own machine",
        )
    try:
        credentials.find(control_plane.CONTROL_PLANE_TOKEN)  # can the keyring be used at all?
    except CredentialError as error:
        raise CliError(
            str(error), ExitCode.ERROR, hint="fix the keyring first: the link works once"
        ) from error
    with control_plane.ControlPlaneClient(
        base, None, transport=control_plane.make_transport()
    ) as client:
        got = client.redeem_invite(token)
    stored = [control_plane.CONTROL_PLANE_TOKEN]
    try:
        credentials.store(control_plane.CONTROL_PLANE_TOKEN, got.control_plane_token)
        if got.gateway_key:
            credentials.store(TOKENLEAN_KEY, got.gateway_key)
            stored.append(TOKENLEAN_KEY)
    except CredentialError as error:
        raise CliError(
            f"the invite was used but its keys could not be stored: {error}",
            ExitCode.ERROR,
            hint="ask your team lead or org admin for a new invite",
        ) from error
    control_plane.record_login(home, base, got.tenant.slug, got.email)
    settings = user_config(home)
    config = {}
    if control_plane.control_plane_url(os.environ, settings) != base:
        config["control_plane_url"] = base
    if got.gateway_url and (settings.get("gateway_url") or "").rstrip("/") != got.gateway_url:
        config["gateway_url"] = got.gateway_url
    data = {
        "stored": stored,
        "where": "keyring",
        "tenant": got.tenant.slug,
        "user": got.email,
        "url": base,
        "gateway_key_id": got.gateway_key_id,
        "token_expires_at": got.token_expires_at.isoformat(),
        "config": config,
        "notice": got.notice,
    }

    def show(console) -> None:
        console.print(
            f"Signed in to {base} ({got.tenant.name}) as {got.email}; "
            f"{' and '.join(stored)} stored in the OS keyring.",
            markup=False,
        )
        if got.notice:
            console.print(got.notice, markup=False)
        if config:
            console.print("Add to ~/.agentic/config.yaml:", markup=False)
            for key, value in config.items():
                console.print(f"  {key}: {value}", markup=False)
        console.print("Next: cd <repo> and run `agentic init`.", markup=False)

    out.result(data, show)


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
    settings = user_config(agentic_home())
    if wanted == JIRA:
        return login_credential(settings), "Jira token"
    names = login_names(settings)
    if wanted not in names:
        choices = ", ".join([GATEWAY, CONTROL_PLANE, JIRA, *sorted(names)])
        raise CliError(
            f"--for {target!r} is not something agentic keeps a key for",
            ExitCode.USAGE,
            hint=f"choose one of: {choices}",
        )
    return names[wanted], f"Token for {wanted}"
