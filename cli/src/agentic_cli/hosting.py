"""Which hosting provider serves a repository, and with which token (M1-58, spec §16).

The registry is the one place that knows provider kinds. `HOST_KINDS` maps a kind to
its adapter, its API URL rule and the credential its token is stored under; a new
provider (GitLab, Azure DevOps) is one entry here plus its adapter module - the engine,
the publish flow and the commands do not change.

Hosts: github.com is built in. Any other host - GitHub Enterprise Server - is declared
in the user config (`~/.agentic/config.yaml`), so a token is never sent to a host
nobody named:

    git_hosts:
      github.acme.com:
        kind: github
        api_url: https://github.acme.com/api/v3   # optional; this is the default
        credential: ghe_token                     # optional; default github_token

The token comes from `agentic.auth.Credentials`: `AGENTIC_<CREDENTIAL>` in the
environment first (CI, service accounts), then the OS keyring (`agentic login --for
github`). It is held only in memory and never shown.
"""

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic.auth import CredentialError, Credentials, env_var
from agentic.hosting import HostingError, HostingProvider, RepoRef, parse_remote
from agentic.hosting.git import remote_url
from agentic.hosting.github import GITHUB_COM, GitHubHost, api_url_for
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

_CREDENTIAL = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HOST_KEYS = frozenset({"kind", "api_url", "credential"})


@dataclass(frozen=True)
class HostKind:
    """One provider kind: how to reach its API and where its token is kept."""

    make: Callable[[str, str], HostingProvider]  # (api_url, token) -> provider
    api_url: Callable[[str, str | None], str]  # (host, configured api_url) -> API root
    credential: str  # the default credential name


HOST_KINDS: dict[str, HostKind] = {
    "github": HostKind(make=GitHubHost, api_url=api_url_for, credential="github_token"),
}
BUILT_IN_HOSTS = {GITHUB_COM: "github"}


@dataclass(frozen=True)
class HostConfig:
    host: str
    kind: str
    api_url: str
    credential: str


@dataclass
class Connection:
    """The provider for one repository's remote, ready to call."""

    provider: HostingProvider
    repo: RepoRef
    remote: str
    kind: str
    token: str = field(repr=False)  # for scrubbing messages; never shown
    token_source: str = ""  # "env" or "keyring"
    token_hint: str = ""  # how to provide (or replace) the token

    @contextmanager
    def errors(self, *, keep_transient: bool = False) -> Iterator[None]:
        """A failed hosting call (push or API) as a CliError, with what to do about it.
        `keep_transient` lets a failure that may pass on its own through unchanged, for
        a caller that tries again (a CI watch)."""
        try:
            yield
        except HostingError as error:
            if keep_transient and error.transient:
                raise
            status = getattr(error, "status", None)
            hint = self.token_hint if status in (401, 403) else None
            raise CliError(str(error), ExitCode.ERROR, hint=hint) from error

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def default_credentials() -> Credentials:
    """Where tokens are looked up (tests replace it)."""
    return Credentials()


def host_configs(settings: Mapping[str, Any]) -> dict[str, HostConfig]:
    """Every known host, by lower-case name: the built-in ones and `git_hosts`."""
    found = {
        host: HostConfig(host, kind, HOST_KINDS[kind].api_url(host, None), _default(kind))
        for host, kind in BUILT_IN_HOSTS.items()
    }
    declared = settings.get("git_hosts") or {}
    if not isinstance(declared, Mapping):
        raise _invalid("git_hosts must map host names to settings")
    for name, entry in declared.items():
        host = str(name).strip().lower()
        if not isinstance(entry, Mapping):
            raise _invalid(f"git_hosts.{name} must be a mapping (kind, api_url, credential)")
        unknown = sorted(set(entry) - _HOST_KEYS)
        if unknown:
            raise _invalid(f"git_hosts.{name}: unknown setting(s) {', '.join(unknown)}")
        kind = str(entry.get("kind") or "")
        if kind not in HOST_KINDS:
            known = ", ".join(sorted(HOST_KINDS))
            raise _invalid(f"git_hosts.{name}: kind {kind!r} is not one of: {known}")
        credential = str(entry.get("credential") or _default(kind))
        if not _CREDENTIAL.fullmatch(credential):
            raise _invalid(f"git_hosts.{name}: credential must be a lower-case slug (a-z, 0-9, _)")
        configured = entry.get("api_url")
        api_url = HOST_KINDS[kind].api_url(host, str(configured) if configured else None)
        found[host] = HostConfig(host, kind, api_url, credential)
    return found


def connect(
    where: Path,
    settings: Mapping[str, Any],
    *,
    remote: str = "origin",
    credentials: Credentials | None = None,
) -> Connection:
    """The provider and repository for `remote` of the repository at `where`. Refused
    (exit 2) when the remote is not on a known host or no token is available."""
    try:
        url = remote_url(where, remote)
        repo = parse_remote(url)
    except HostingError as error:
        raise CliError(
            str(error),
            ExitCode.REFUSED,
            hint=f"the pull request needs a {remote} remote on a hosting service",
        ) from error
    config = host_configs(settings).get(repo.host)
    if config is None:
        raise CliError(
            f"{repo.host} is not a known git host, so no token is sent to it",
            ExitCode.REFUSED,
            hint="declare it under git_hosts in ~/.agentic/config.yaml "
            "(kind: github for GitHub Enterprise), or open the pull request by hand",
        )
    found = _token(config, credentials or default_credentials())
    try:
        provider = HOST_KINDS[config.kind].make(config.api_url, found.value)
    except HostingError as error:
        raise CliError(str(error), ExitCode.INVALID, hint="fix api_url in git_hosts") from error
    return Connection(
        provider, repo, remote, config.kind, found.value, found.source, _token_hint(config)
    )


def login_names(settings: Mapping[str, Any]) -> dict[str, str]:
    """`agentic login --for <name>` choices -> the credential stored: each provider kind
    and each configured host."""
    names = {kind: entry.credential for kind, entry in HOST_KINDS.items()}
    names.update({host: config.credential for host, config in host_configs(settings).items()})
    return names


def _token(config: HostConfig, found_in: Credentials):
    try:
        return found_in.get(config.credential, login=_login(config))
    except CredentialError as error:
        raise CliError(
            f"no token for {config.host}: {error}", ExitCode.REFUSED, hint=_token_hint(config)
        ) from error


def _login(config: HostConfig) -> str:
    """The command that stores this host's token."""
    name = config.kind if config.credential == _default(config.kind) else config.host
    return f"agentic login --for {name}"


def _token_hint(config: HostConfig) -> str:
    return (
        f"set {env_var(config.credential)}, or run `{_login(config)}` "
        f"(a token that may open pull requests on {config.host})"
    )


def _default(kind: str) -> str:
    return HOST_KINDS[kind].credential


def _invalid(message: str) -> CliError:
    return CliError(message, ExitCode.INVALID, hint="fix git_hosts in ~/.agentic/config.yaml")
