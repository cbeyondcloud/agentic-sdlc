"""Where the CLI keeps keys (DF-955): the OS keyring first, the credential file only where
there is no keyring and nobody forbade it.

Windows developers work in WSL2 (D38), which usually has no Secret Service, so `keyring`
has no backend there. Like the GitHub CLI, agentic then keeps keys in
`~/.agentic/credentials.json` - readable only by the user, not encrypted - says so at
`agentic login`, and shows it in `agentic doctor`. Every command builds its credential
provider here (`cli_credentials`), so they all agree.

The choice, `credential_store`, from `AGENTIC_CREDENTIAL_STORE` or the user config:

  * `auto` (the default): the keyring; the file only when no keyring backend exists, and
    a key already in the file is still found;
  * `keyring`: never the file - how a managed laptop forbids it from the first login;
  * `file`: the file, skipping the keyring (a keyring that exists but cannot be used).

An org forbids the file with `credential_file_fallback: false` in `policy/org.yaml`; the
CLI reads it from the org policy cached on this machine (`agentic init`). A cached policy
that cannot be read forbids it too.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentic.auth import FILE_NAME, Credentials, FileStore
from agentic.yamlio import YamlFileError, load_yaml, read_text
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

STORE_ENV = "AGENTIC_CREDENTIAL_STORE"
SETTING = "credential_store"
MODES = ("auto", "keyring", "file")
POLICY_KEY = "credential_file_fallback"


@dataclass(frozen=True)
class StoreChoice:
    mode: Literal["auto", "keyring", "file"]
    path: Path  # the credential file, used or not
    forbidden: str | None  # who forbids the file (org policy), or None

    @property
    def file_allowed(self) -> bool:
        return self.mode != "keyring" and self.forbidden is None


def store_choice(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    settings: Mapping[str, Any] | None = None,
) -> StoreChoice:
    """The person's choice and the org's word on it. CliError (invalid) for an unknown
    choice. A user config that cannot be read counts as no choice."""
    env = os.environ if env is None else env
    home = agentic_home(env) if home is None else home
    if settings is None:
        try:
            settings = user_config(home)
        except CliError:
            settings = {}
    named = env.get(STORE_ENV, "").strip().lower() or str(settings.get(SETTING) or "auto")
    mode = named.strip().lower()
    if mode not in MODES:
        raise CliError(
            f"credential_store {named!r} is not one of auto, keyring or file",
            ExitCode.INVALID,
            hint=f"fix {SETTING} in ~/.agentic/config.yaml or {STORE_ENV}",
        )
    return StoreChoice(mode, home / FILE_NAME, _forbidden(home, settings))  # type: ignore[arg-type]


def cli_credentials(env: Mapping[str, str] | None = None, home: Path | None = None) -> Credentials:
    """The credential provider every command uses: the environment, the OS keyring, and
    the credential file where `store_choice` allows it."""
    env = os.environ if env is None else env
    choice = store_choice(env, home)
    files = FileStore(choice.path) if choice.file_allowed else None
    return Credentials(environ=env, file_store=files, prefer_file=choice.mode == "file")


def forbidden_file_warning(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> str | None:
    """What `agentic login` and `agentic init` warn about once (DF-960): a credential file
    still on disk although it may no longer be used - written before the org policy that
    forbids it was cached, or before `credential_store: keyring`. None otherwise."""
    try:
        choice = store_choice(env, home)
        present = choice.path.is_file() or choice.path.is_symlink()
    except (CliError, OSError):  # an unknown choice or an unreadable folder: doctor says so
        return None
    if choice.file_allowed or not present:
        return None
    why = choice.forbidden or "credential_store is keyring"
    return (
        f"{choice.path} still holds keys, but {why}, so they are not used; delete it "
        f"(`rm {choice.path}`) and store your keys in the OS keyring with `agentic login` "
        "(or set the AGENTIC_* variables)"
    )


def _forbidden(home: Path, settings: Mapping[str, Any]) -> str | None:
    org = str(settings.get("org") or "").strip()
    if not org:
        return None
    path = home / "policy" / org / "repo" / "policy" / "org.yaml"
    if not path.is_file():
        return None
    try:
        data, _ = load_yaml(read_text(path))
    except (OSError, YamlFileError):
        return f"org {org}'s cached policy ({path}) cannot be read, so {POLICY_KEY} is unknown"
    value = data.get(POLICY_KEY, True) if isinstance(data, dict) else True
    if value is True:
        return None
    return f"org {org}'s policy ({POLICY_KEY}: {str(value).lower()})"
