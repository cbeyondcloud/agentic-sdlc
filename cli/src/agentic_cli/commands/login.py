"""`agentic login`: store the user's gateway key in the OS keyring (spec §24 step 3, M1-53).

The key goes through `agentic.auth.Credentials.store` into the Windows Credential
Manager, the macOS Keychain or the Secret Service; every later command finds it there
(the `AGENTIC_TOKENLEAN_API_KEY` environment variable still wins). The key is read with
no echo, or from standard input with `--key-stdin` for scripts, and never printed.
In CI there is no keyring to use, so login is refused with what to do instead.

Verifying the key with the gateway and registering with the control plane come with
the control plane (M2); until then login says the key was stored, not verified.
"""

import sys

import typer

from agentic.auth import TOKENLEAN_KEY, CredentialError, Credentials, env_var
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output


def register(app: typer.Typer) -> None:
    @app.command("login")
    def login(
        ctx: typer.Context,
        key_stdin: bool = typer.Option(
            False, "--key-stdin", help="Read the key from standard input instead of a prompt."
        ),
    ) -> None:
        """Store your gateway key in the OS keyring."""
        out: Output = ctx.obj
        credentials = Credentials()
        if credentials.in_ci:
            raise CliError(
                f"login stores keys in the OS keyring, which is not used in CI "
                f"({credentials.ci_marker} is set)",
                ExitCode.REFUSED,
                hint=f"set {env_var(TOKENLEAN_KEY)} from the CI system's secrets instead",
            )
        if key_stdin:
            value = sys.stdin.readline().strip()
        else:
            value = typer.prompt("Gateway key", hide_input=True).strip()
        if not value:
            raise CliError("no key given", ExitCode.INVALID, hint="paste the key from your invite")
        try:
            credentials.store(TOKENLEAN_KEY, value)
        except CredentialError as error:
            raise CliError(
                str(error), ExitCode.ERROR, hint=f"or set {env_var(TOKENLEAN_KEY)}"
            ) from error
        data = {"stored": TOKENLEAN_KEY, "where": "keyring", "verified": False}
        out.result(
            data,
            lambda c: c.print(
                "Key stored in the OS keyring (not yet verified with the gateway). "
                "Next: cd <repo> and run `agentic init`."
            ),
        )
