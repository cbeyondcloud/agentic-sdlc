"""Starting a run from a Jira issue and reporting back to it (M2-50, spec §16).

The site is declared in the user config (`~/.agentic/config.yaml`), so a token is never
sent to a site nobody named:

    jira:
      url: https://acme.atlassian.net      # or a Data Center URL, e.g. https://jira.acme.example/jira
      user: you@acme.example               # Cloud: the account email (with an API token);
                                           # Data Center: leave out for a personal access token
      credential: jira_token               # optional; the default
      api_version: 2                       # optional; 3 for Cloud's v3 (ADF comments)
      transition: In Progress              # optional; what `run --from-jira` moves the issue to

`AGENTIC_JIRA_URL` and `AGENTIC_JIRA_USER` override `url` and `user` (CI, service
accounts). The token comes from `agentic.auth.Credentials`: `AGENTIC_JIRA_TOKEN` (or
`AGENTIC_<CREDENTIAL>`) first, then the OS keyring (`agentic login --for jira`). It is held
only in memory, never shown, logged or written to the run folder.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentic.auth import CredentialError, Credentials, env_var
from agentic.integrations.jira import API_VERSIONS, JiraClient, JiraError, is_cloud
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

JIRA = "jira"  # `agentic login --for jira`
JIRA_TOKEN = "jira_token"
URL_ENV = "AGENTIC_JIRA_URL"
USER_ENV = "AGENTIC_JIRA_USER"
LOGIN = "agentic login --for jira"
_KEYS = frozenset({"url", "user", "credential", "api_version", "transition"})
_CREDENTIAL = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CONFIG_HINT = "fix jira in ~/.agentic/config.yaml"


@dataclass(frozen=True)
class JiraSettings:
    url: str
    user: str | None
    credential: str = JIRA_TOKEN
    api_version: str = "2"
    transition: str | None = None  # moved to when a run starts from an issue


def jira_settings(settings: Mapping[str, Any], env: Mapping[str, str]) -> JiraSettings:
    """The Jira site from the user config and the environment. CliError (invalid) when
    none is configured or a setting cannot work."""
    declared = settings.get("jira")
    if declared is None:
        declared = {}
    if not isinstance(declared, Mapping):
        raise _invalid("jira must be a mapping (url, user, credential, api_version, transition)")
    unknown = sorted(set(declared) - _KEYS)
    if unknown:
        raise _invalid(f"jira: unknown setting(s) {', '.join(map(str, unknown))}")
    url = (env.get(URL_ENV, "").strip() or str(declared.get("url") or "").strip()).rstrip("/")
    if not url:
        raise CliError(
            "no Jira site configured",
            ExitCode.INVALID,
            hint=f"set jira.url in ~/.agentic/config.yaml (https://<site>.atlassian.net, or "
            f"your Data Center URL) or {URL_ENV}",
        )
    user = env.get(USER_ENV, "").strip() or str(declared.get("user") or "").strip() or None
    credential = str(declared.get("credential") or JIRA_TOKEN)
    if not _CREDENTIAL.fullmatch(credential):
        raise _invalid("jira.credential must be a lower-case slug (a-z, 0-9, _)")
    api_version = str(declared.get("api_version") or "2")
    if api_version not in API_VERSIONS:
        raise _invalid(f"jira.api_version must be one of {', '.join(API_VERSIONS)}")
    transition = declared.get("transition")
    if transition is not None and not str(transition).strip():
        raise _invalid("jira.transition, when set, names a transition or a status")
    if is_cloud(url) and user is None:
        raise CliError(
            f"{url} is a Jira Cloud site: it needs the account's email as jira.user",
            ExitCode.INVALID,
            hint=f"set jira.user in ~/.agentic/config.yaml (or {USER_ENV}) to the email "
            "of the account the API token belongs to",
        )
    return JiraSettings(
        url=url,
        user=user,
        credential=credential,
        api_version=api_version,
        transition=str(transition).strip() if transition is not None else None,
    )


def default_credentials() -> Credentials:
    """Where the token is looked up (tests replace it)."""
    return Credentials()


def connect_jira(ws, credentials: Credentials | None = None) -> JiraClient:
    """A client for the configured site. Refused (exit 2) without a token; invalid (exit 3)
    for settings that cannot work."""
    found = jira_settings(ws.settings, ws.env)
    try:
        token = (credentials or default_credentials()).get(found.credential, login=LOGIN)
    except CredentialError as error:
        raise CliError(
            f"no Jira token: {error}", ExitCode.REFUSED, hint=token_hint(found)
        ) from error
    try:
        return JiraClient(found.url, token.value, user=found.user, api_version=found.api_version)
    except JiraError as error:
        raise CliError(str(error), ExitCode.INVALID, hint=_CONFIG_HINT) from error


def token_hint(found: JiraSettings) -> str:
    kind = "an API token" if is_cloud(found.url) else "a personal access token"
    return f"set {env_var(found.credential)}, or run `{LOGIN}` ({kind} for {found.url})"


def login_credential(settings: Mapping[str, Any]) -> str:
    """The credential `agentic login --for jira` stores: the configured one, else the
    default. A broken `jira` section stores under the default name rather than failing."""
    declared = settings.get("jira")
    if isinstance(declared, Mapping):
        named = str(declared.get("credential") or "")
        if _CREDENTIAL.fullmatch(named):
            return named
    return JIRA_TOKEN


def _invalid(message: str) -> CliError:
    return CliError(message, ExitCode.INVALID, hint=_CONFIG_HINT)
