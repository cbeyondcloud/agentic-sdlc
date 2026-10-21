"""`agentic login`: store the user's gateway key in the OS keyring (spec §24 step 3, M1-53),
or, with `--for github` (or a host from `git_hosts`), the hosting token that opens pull
requests (M1-58).

The key goes through `agentic.auth.Credentials.store` into the Windows Credential
Manager, the macOS Keychain or the Secret Service; every later command finds it there
(the `AGENTIC_TOKENLEAN_API_KEY` / `AGENTIC_GITHUB_TOKEN` environment variable still
wins). The key is read with no echo, or from standard input with `--key-stdin` for
scripts, and never printed. In CI there is no keyring to use, so login is refused with
what to do instead.

Verifying the key with the gateway and registering with the control plane come with
the control plane (M2); until then login says the key was stored, not verified.
"""

import sys

import typer

from agentic.auth import TOKENLEAN_KEY, CredentialError, Credentials, env_var
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import login_names
from agentic_cli.output import Output

GATEWAY = "gateway"


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
            help="What the key is for: gateway (default), a git host kind such as github, "
            "or a host from git_hosts.",
        ),
    ) -> None:
        """Store your gateway key (or a git host token) in the OS keyring."""
        out: Output = ctx.obj
        name, label = _credential(target)
        credentials = Credentials()
        if credentials.in_ci:
            raise CliError(
                f"login stores keys in the OS keyring, which is not used in CI "
                f"({credentials.ci_marker} is set)",
                ExitCode.REFUSED,
                hint=f"set {env_var(name)} from the CI system's secrets instead",
            )
        if key_stdin:
            value = sys.stdin.readline().strip()
        else:
            value = typer.prompt(label, hide_input=True).strip()
        if not value:
            raise CliError("no key given", ExitCode.INVALID, hint="paste the key from your invite")
        try:
            credentials.store(name, value)
        except CredentialError as error:
            raise CliError(str(error), ExitCode.ERROR, hint=f"or set {env_var(name)}") from error
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


def _credential(target: str) -> tuple[str, str]:
    """(credential name, prompt label) for `--for <target>`."""
    wanted = target.strip().lower()
    if wanted == GATEWAY:
        return TOKENLEAN_KEY, "Gateway key"
    names = login_names(user_config(agentic_home()))
    if wanted not in names:
        choices = ", ".join([GATEWAY, *sorted(names)])
        raise CliError(
            f"--for {target!r} is not something agentic keeps a key for",
            ExitCode.USAGE,
            hint=f"choose one of: {choices}",
        )
    return names[wanted], f"Token for {wanted}"
