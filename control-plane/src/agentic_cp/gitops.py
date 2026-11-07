"""git for the control plane: the one place a git process is started (policy repos, CODEOWNERS).

Every command runs with line-ending conversion and fsmonitor off, the `ext::` transport
refused, no terminal prompt, and - when `<git_config_dir>/<tenant-slug>.gitconfig` exists -
that tenant's own global git config (a mounted secret naming a credential helper, ND-15).
Credentials never sit in a stored URL (`check_repo_url` refuses one), and git's error text
is scrubbed of URL credentials before it is logged or returned.

Repository URLs a tenant gives must be remote (R12): `https://`, `ssh://` or the scp form
`user@host:path`. A `file://` URL or a path would let a tenant admin read the server's
disk - another tenant's policy work folder included - so it is accepted only when the
deployment sets `AGENTIC_CP_ALLOW_LOCAL_REPOS` (development and tests). A remote URL must
also be unambiguous: no '#', '?' or '\\', and at most one '@', in the authority - otherwise
git fetches from one host while the repo key and `allowed_repo_patterns` see another
(review F1).

A remote URL's host must also be one the org policy allows (DF-484, DF-503): `GitHosts`
is the tenant's list and `check_git_host` the check, made when a URL is set and again
before every fetch (DNS can change). A host listed by name is fetched whatever it resolves
to (an org's own git server on its private network); any other allowed host must resolve
to public addresses only, never to a private, loopback, link-local, unspecified, multicast
or reserved one (IPv4 and IPv6, an IPv4 address mapped or embedded in IPv6 included).
git's own error text is logged, never handed to a caller (`GitError.reason`)."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from agentic.hosting.base import scrub_url_credentials
from agentic.policy.repos import git_host_key, normalise_remote
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
    # git never checks a work tree out here (bare repos, `archive` into memory), and every
    # name is judged by `portable_path_problems` instead: off, a Windows-hosted server
    # names an unportable file (policy.path) as a Linux one does, rather than failing to
    # read the tree at all (review F4)
    "-c",
    "core.protectNTFS=false",
)
#: Inherited GIT_* variables that only shape the network: the deployment's CA bundle and
#: proxy. Everything else - GIT_DIR, GIT_CONFIG_PARAMETERS, GIT_SSH_COMMAND (an ssh key) -
#: is dropped: a credential reaches git only through the tenant's own config (R25).
_KEPT_GIT_VARS = frozenset({"GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_PROXY_COMMAND"})
#: Everything else git and ssh may inherit (DF-496): path, home, locale, temp folders,
#: proxies, CA bundles and what Windows needs to start a process. The server's own settings
#: (its DSNs, SMTP password, gateway key) never reach a child.
_KEPT_VARS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
    }
    | _KEPT_GIT_VARS
)


def _kept(name: str) -> bool:
    upper = name.upper()  # proxies are often lower case; Windows names any case
    return upper in _KEPT_VARS or upper.startswith("LC_")


#: ssh without the service account's own keys, agent or config: a tenant without its own
#: git config fetches anonymously (its config may set `core.sshCommand` with its key).
_ANONYMOUS_SSH = (
    "ssh -F none -o IdentitiesOnly=yes -o IdentityFile=none -o IdentityAgent=none -o BatchMode=yes"
)
_SCP = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:(?!\\)[^\s\\]+")
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")


class RepoUrlError(ValueError):
    """A repository URL was refused. `credentials` says whether it carried any."""

    def __init__(self, message: str, *, credentials: bool = False) -> None:
        super().__init__(message)
        self.credentials = credentials


#: What a caller is told when git itself failed: git's own line (which may say whether an
#: internal host answered) is logged, never returned (DF-503).
GIT_FAILED = "git could not fetch the repository; the control plane's log has git's message"


class GitError(Exception):
    """A git command failed; the message is already scrubbed of credentials. It is for the
    server's log: `reason` is what a caller may be told."""

    def __init__(self, message: str, *, reason: str = GIT_FAILED) -> None:
        super().__init__(message)
        self.reason = reason

    @classmethod
    def refused(cls, error: RepoUrlError) -> GitError:
        """A URL refused before git ran: our own message, safe to show."""
        return cls(str(error), reason=str(error))


def check_repo_url(url: str, *, allow_local: bool) -> str:
    """`url`, stripped, when a tenant may use it (R12, ND-15); `RepoUrlError` otherwise.
    The message never repeats the URL (it may hold a secret)."""
    value = url.strip()
    if not value or value.startswith("-") or _UNSAFE.search(value):
        raise RepoUrlError("a repository URL is one https://, ssh:// or user@host:path URL")
    if "://" in value:
        try:
            parts = urlsplit(value)
            host = parts.hostname
            _ = parts.port  # a malformed port raises here
        except ValueError:
            raise RepoUrlError("the repository URL does not parse as a URL") from None
        scheme = parts.scheme.lower()
        if parts.password or (scheme in ("https", "http") and parts.username):
            raise RepoUrlError(
                "the repository URL carries credentials; give the plain URL and mount git "
                "credentials for the tenant instead",
                credentials=True,
            )
        if scheme in ("https", "ssh") and host and _same_host(value, parts.netloc, host):
            return value
        if scheme == "file" and allow_local:
            return value
        raise RepoUrlError("a repository URL must use https:// or ssh:// (or user@host:path)")
    if _SCP.fullmatch(value) and _same_host(value, value.partition(":")[0], None):
        return value
    if _is_path(value):
        if allow_local:
            return value
        raise RepoUrlError("a local path is not a repository URL this server may fetch")
    raise RepoUrlError("a repository URL must use https:// or ssh:// (or user@host:path)")


def _same_host(value: str, authority: str, host: str | None) -> bool:
    """Whether a remote URL is unambiguous (review F1): no '#', '?' or '\\' (a fragment,
    a query or a Windows separator moves where git and a regex see the host), at most one
    '@' and only in the authority, no user or host starting with '-' (an ssh option), and
    the registration key's host is the host git connects to."""
    if any(char in value for char in "#?\\") or value.count("@") > 1:
        return False
    if "@" in value and "@" not in authority:
        return False  # an '@' in the path
    user, _, name = authority.rpartition("@")
    if user.startswith("-") or name.startswith("-"):
        return False
    expected = host if host is not None else name.lower()
    return normalise_remote(value).split("/", 1)[0] == expected


def _is_path(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def scrub(text: str) -> str:
    return scrub_url_credentials(text)


# ── which hosts (DF-484, DF-503) ─────────────────────────────────────────────

Resolver = Callable[[str], Sequence[str]]


@dataclass(frozen=True)
class GitHosts:
    """The hosts a tenant's git URLs may name. `explicit` ones are listed by name and never
    resolved; `implicit` ones (the defaults while org policy lists none) must resolve to
    public addresses only."""

    explicit: frozenset[str] = frozenset()
    implicit: frozenset[str] = frozenset()


def url_host(url: str) -> str | None:
    """The host git connects to for a URL `check_repo_url` accepted, as `git_host_key`
    writes it; None for a local repo (a `file://` URL or a path). `ValueError` when the
    host cannot be read."""
    value = url.strip()
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme.lower() == "file":
            return None
        if not parts.hostname:
            raise ValueError("the URL names no host")
        return git_host_key(parts.hostname)
    if _SCP.fullmatch(value):
        return git_host_key(value.partition(":")[0].rpartition("@")[2])
    return None


def resolve_addresses(host: str) -> list[str]:
    """Every address `host` resolves to now (an IP literal resolves to itself)."""
    found = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in found})


#: NAT64's well-known prefix: the last 32 bits are an IPv4 address (RFC 6052).
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def blocked_address(address: str) -> bool:
    """Whether an address is not a public unicast one: private, loopback, link-local,
    unspecified, multicast, reserved or shared, an IPv4 address mapped or embedded in IPv6
    (mapped, 6to4, NAT64) judged as that IPv4 address. One that does not parse is blocked."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            ip = embedded
    return ip.is_multicast or not ip.is_global


def check_git_host(url: str, hosts: GitHosts, *, resolve: Resolver = resolve_addresses) -> None:
    """`RepoUrlError` unless git may fetch `url` from its host now. A local repo has no host
    (`check_repo_url` decides whether one is allowed at all). The message names the host,
    never the URL or an address it resolved to."""
    try:
        host = url_host(url)
    except ValueError:
        raise RepoUrlError("the repository URL names no host the control plane can check") from None
    if host is None or host in hosts.explicit:
        return
    refused = RepoUrlError(
        f"the git host {host} is not allowed: an org admin lists the hosts the control "
        "plane may fetch from in the org policy (org.yaml allowed_git_hosts)"
    )
    if host not in hosts.implicit:
        raise refused
    try:
        addresses = list(resolve(host))
    except (OSError, UnicodeError):
        raise refused from None
    if not addresses or any(blocked_address(address) for address in addresses):
        raise refused


class GitRunner:
    """Runs git for one tenant at a time. Tests subclass it and override `_spawn`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def resolve(self, host: str) -> Sequence[str]:
        """The addresses `host` resolves to (`check_git_host`); tests override it."""
        return resolve_addresses(host)

    def check_host(self, url: str, hosts: GitHosts) -> None:
        """`check_git_host` with this runner's resolver; `RepoUrlError` when refused."""
        check_git_host(url, hosts, resolve=self.resolve)

    def env(self, tenant_slug: str | None) -> dict[str, str]:
        """git's environment for one tenant (R25): its own `<slug>.gitconfig` as the global
        config when mounted, else none at all; never the system config, the service
        account's `~/.gitconfig`, credential helpers or ssh keys - one tenant must not
        fetch another tenant's private repo with credentials the server happens to hold."""
        env = {key: value for key, value in os.environ.items() if _kept(key)}
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
            raise GitError(scrub(str(error))[:300], reason=error.reason) from None

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
