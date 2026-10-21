"""The hosting provider seam (M1-58, spec §16): where a run's branch becomes a pull request.

GitHub is the v1 adapter (`agentic.hosting.github`); GitLab, Azure DevOps or Bitbucket
are later adapters behind this same protocol, so neither the engine nor the CLI's
publish flow changes when one is added. The protocol is deliberately small: what
Zyroflo's release flow needs up to - and never including - the merge click:

  * `default_branch` - where a pull request goes when nothing else says;
  * `find_pull_request` / `open_pull_request` / `update_pull_request` - publishing is
    idempotent: a second publish updates the open pull request's body;
  * `checks` - every CI check on a commit, in one vocabulary (below), for the verdict.

Check vocabulary (an adapter maps its platform's words into these): `status` is
`queued`, `in_progress`, `pending` or `completed`; `conclusion` is None until
completed, then `success`, `failure`, `cancelled`, `skipped`, `neutral`, `timed_out`
or `action_required`. A word outside these passes through unchanged, and the verdict
names it as unknown rather than guessing a colour for it.

There is deliberately no merge operation: a person merges.
"""

import re
from dataclasses import dataclass
from typing import Protocol


class HostingError(Exception):
    """A hosting call failed or was refused. The message never holds a token or a
    credential from a URL, and says what to do where it can."""


@dataclass(frozen=True)
class RepoRef:
    """A hosted repository: `host` (lower case, no port) and `owner/name`."""

    host: str
    owner: str  # may hold "/" on platforms with nested groups
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str  # the page a person opens
    head: str  # the branch it proposes
    base: str  # the branch it would merge into
    head_sha: str  # the commit CI checks
    state: str = "open"


@dataclass(frozen=True)
class CheckRun:
    """One CI check on a commit, in the vocabulary above."""

    name: str
    status: str
    conclusion: str | None
    link: str = ""


class HostingProvider(Protocol):
    kind: str  # "github", ...: what the host registry calls this adapter

    def default_branch(self, repo: RepoRef) -> str: ...

    def find_pull_request(self, repo: RepoRef, head: str) -> PullRequest | None: ...

    def open_pull_request(
        self, repo: RepoRef, *, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...

    def update_pull_request(self, repo: RepoRef, number: int, *, body: str) -> PullRequest: ...

    def checks(self, repo: RepoRef, sha: str) -> list[CheckRun]: ...


_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.DOTALL)
_SCP = re.compile(r"^(?:[^@/\\]+@)?([^/:\\]+):(.+)$", re.DOTALL)
_NETWORK_SCHEMES = frozenset({"http", "https", "ssh", "git", "git+ssh", "ssh+git"})


def parse_remote(url: str) -> RepoRef:
    """The repository a git remote URL names (https, ssh or scp form). Credentials in
    the URL are dropped, never kept. A local path is a HostingError: it names no host."""
    text = (url or "").strip()
    if not text or "\\" in text or re.match(r"^[A-Za-z]:", text):
        raise HostingError(f"the remote {_shown(text)} is a local path, not a hosted repository")
    scheme = _SCHEME.match(text)
    if scheme:
        if scheme.group(1).lower() not in _NETWORK_SCHEMES:
            raise HostingError(f"the remote {_shown(text)} is not a hosted repository")
        netloc, _, path = scheme.group(2).partition("/")
    elif scp := _SCP.match(text):
        netloc, path = scp.group(1), scp.group(2)
    else:
        raise HostingError(f"the remote {_shown(text)} is a local path, not a hosted repository")
    host = netloc.rsplit("@", 1)[-1]  # userinfo is not the host (and is never kept)
    host = re.sub(r":\d*$", "", host).lower()
    parts = [p for p in path.strip("/").removesuffix(".git").split("/") if p]
    if not host or len(parts) < 2:
        raise HostingError(f"the remote {_shown(text)} names no owner/name repository on a host")
    return RepoRef(host=host, owner="/".join(parts[:-1]), name=parts[-1])


def scrub_url_credentials(text: str) -> str:
    """`text` with the userinfo of every URL in it removed (`https://user:pw@h` -> `https://h`)."""
    return re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s'\"]+@", r"\1", text)


def _shown(text: str) -> str:
    """A remote as it may be shown: its credentials removed."""
    return repr(scrub_url_credentials(text))
