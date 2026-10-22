"""git for the control plane: the one place a git process is started (policy repos, CODEOWNERS).

Every command runs with line-ending conversion and fsmonitor off, the `ext::` transport
refused, no terminal prompt, and - when `<git_config_dir>/<tenant-slug>.gitconfig` exists -
that tenant's own global git config (a mounted secret naming a credential helper, ND-15).
Credentials never sit in a stored URL (`check_repo_url` refuses one), and git's error text
is scrubbed of URL credentials before it is logged or returned.

Repository URLs a tenant gives must be remote (R12): `https://`, `ssh://` or the scp form
`user@host:path`. A `file://` URL or a path would let a tenant admin read the server's
disk - another tenant's policy work folder included - so it is accepted only when the
deployment sets `AGENTIC_CP_ALLOW_LOCAL_REPOS` (development and tests)."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from agentic.hosting.base import scrub_url_credentials
from agentic_cp.settings import Settings

FLAGS = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.eol=lf",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.ext.allow=never",
)
#: Inherited GIT_* variables that only shape the network: the deployment's CA bundle and
#: proxy. Everything else - GIT_DIR, GIT_CONFIG_PARAMETERS, GIT_SSH_COMMAND (an ssh key) -
#: is dropped: a credential reaches git only through the tenant's own config (R25).
_KEPT_GIT_VARS = frozenset({"GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_PROXY_COMMAND"})
#: ssh without the service account's own keys, agent or config: a tenant without its own
#: git config fetches anonymously (its config may set `core.sshCommand` with its key).
_ANONYMOUS_SSH = (
    "ssh -F none -o IdentitiesOnly=yes -o IdentityFile=none -o IdentityAgent=none -o BatchMode=yes"
)
_SCP = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:(?!\\)[^\s\\]+")
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")


class GitError(Exception):
    """A git command failed; the message is already scrubbed of credentials."""


class RepoUrlError(ValueError):
    """A repository URL was refused. `credentials` says whether it carried any."""

    def __init__(self, message: str, *, credentials: bool = False) -> None:
        super().__init__(message)
        self.credentials = credentials


def check_repo_url(url: str, *, allow_local: bool) -> str:
    """`url`, stripped, when a tenant may use it (R12, ND-15); `RepoUrlError` otherwise.
    The message never repeats the URL (it may hold a secret)."""
    value = url.strip()
    if not value or value.startswith("-") or _UNSAFE.search(value):
        raise RepoUrlError("a repository URL is one https://, ssh:// or user@host:path URL")
    if "://" in value:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if parts.password or (scheme in ("https", "http") and parts.username):
            raise RepoUrlError(
                "the repository URL carries credentials; give the plain URL and mount git "
                "credentials for the tenant instead",
                credentials=True,
            )
        if scheme in ("https", "ssh") and parts.hostname and not parts.hostname.startswith("-"):
            return value
        if scheme == "file" and allow_local:
            return value
        raise RepoUrlError("a repository URL must use https:// or ssh:// (or user@host:path)")
    if _SCP.fullmatch(value) and not value.split("@", 1)[1].startswith("-"):
        return value  # a host starting with '-' would be an ssh option
    if _is_path(value):
        if allow_local:
            return value
        raise RepoUrlError("a local path is not a repository URL this server may fetch")
    raise RepoUrlError("a repository URL must use https:// or ssh:// (or user@host:path)")


def _is_path(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def scrub(text: str) -> str:
    return scrub_url_credentials(text)


class GitRunner:
    """Runs git for one tenant at a time. Tests subclass it and override `_spawn`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def env(self, tenant_slug: str | None) -> dict[str, str]:
        """git's environment for one tenant (R25): its own `<slug>.gitconfig` as the global
        config when mounted, else none at all; never the system config, the service
        account's `~/.gitconfig`, credential helpers or ssh keys - one tenant must not
        fetch another tenant's private repo with credentials the server happens to hold."""
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") or key in _KEPT_GIT_VARS
        }
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_SSH_COMMAND"] = _ANONYMOUS_SSH
        config_dir = self.settings.git_config_dir
        if config_dir is not None and tenant_slug:
            tenant_config = Path(config_dir) / f"{tenant_slug}.gitconfig"
            if tenant_config.is_file():
                env["GIT_CONFIG_GLOBAL"] = str(tenant_config)
                del env["GIT_SSH_COMMAND"]  # the tenant's core.sshCommand, or plain ssh
        return env

    def run(self, args: Sequence[str], *, cwd: Path, tenant_slug: str | None = None) -> bytes:
        """Run `git <args>` in `cwd`; returns stdout. `GitError` on failure."""
        local = "user" if self.settings.allow_local_repos else "never"
        argv = ["git", *FLAGS, "-c", f"protocol.file.allow={local}", *args]
        try:
            return self._spawn(argv, self.env(tenant_slug), cwd, self.settings.git_timeout_seconds)
        except GitError as error:  # every message, whoever raised it, leaves scrubbed
            raise GitError(scrub(str(error))[:300]) from None

    def _spawn(self, argv: Sequence[str], env: Mapping[str, str], cwd: Path, timeout: int) -> bytes:
        try:
            done = subprocess.run(
                list(argv), cwd=cwd, env=dict(env), capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git timed out after {timeout} s") from None
        except OSError as error:
            raise GitError(f"git could not be started ({type(error).__name__})") from None
        if done.returncode != 0:
            lines = [
                line.strip()
                for line in done.stderr.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            raise GitError(lines[-1] if lines else f"git exited with {done.returncode}")
        return done.stdout
