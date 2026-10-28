"""The hosting provider seam (M1-58, spec §16): where a run's branch becomes a pull request.

The v1 adapter lives beside this module; other hosts are later adapters behind this same
protocol, so neither the engine nor the CLI's publish flow changes when one is added
(DF-300: the protocol names no vendor). The protocol is deliberately small: what
Zyroflo's release flow needs up to - and never including - the merge click:

  * `default_branch` - where a pull request goes when nothing else says;
  * `find_pull_request` / `open_pull_request` / `update_pull_request` - publishing is
    idempotent: a second publish updates the open pull request's body, replacing only
    the generated part between `PR_BODY_START` and `PR_BODY_END` (`splice_body`), so
    what a person wrote around it is kept (DF-296);
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
from datetime import datetime
from typing import Protocol, runtime_checkable


class HostingError(Exception):
    """A hosting call failed or was refused. The message never holds a token or a
    credential from a URL, and says what to do where it can.

    `transient`: trying again later may succeed (the network, a 5xx, a rate limit) - a
    watch retries those and fails at once on anything else; `retry_after`: the seconds
    the host asked for before the next try, when it said."""

    def __init__(
        self, message: str, *, transient: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after


class ListingIncomplete(HostingError):
    """The host listed fewer of a pull request's files than it changes (a host caps its
    listing): what was not listed cannot be checked."""


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
    body: str | None = None  # its description as it is now; None when the host did not say
    title: str = ""
    author: str = ""  # the host's login of whoever opened it
    merged: bool = False  # closed by merging (only a single read says so reliably)


@dataclass(frozen=True)
class CheckRun:
    """One CI check on a commit, in the vocabulary above."""

    name: str
    status: str
    conclusion: str | None
    link: str = ""
    log_ref: str = ""  # the host's handle for the check's log; empty when it keeps none


@dataclass(frozen=True)
class BranchProtection:
    """What a host enforces on a branch before a change lands (D33: the policy repo's
    default branch must require review by code owners). `classic_readable` is False when
    the token could not read the host's branch protection itself (some hosts need admin
    rights for it): an unprotected answer is then a "cannot tell", not a "no"."""

    protected: bool  # a pull request (or any rule) is required
    code_owner_reviews: bool  # a code owner must approve
    required_approvals: int
    source: str  # "classic" (branch protection), "rulesets" or "none"
    classic_readable: bool = True


@runtime_checkable
class ProtectionHost(Protocol):
    """Reading a branch's protection (M3-09: `agentic doctor` checks the policy repo)."""

    def branch_protection(self, repo: RepoRef, branch: str) -> BranchProtection: ...


class HostingProvider(Protocol):
    kind: str  # what the host registry calls this adapter

    def default_branch(self, repo: RepoRef) -> str: ...

    def find_pull_request(self, repo: RepoRef, head: str) -> PullRequest | None: ...

    def open_pull_request(
        self, repo: RepoRef, *, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...

    def update_pull_request(self, repo: RepoRef, number: int, *, body: str) -> PullRequest: ...

    def checks(self, repo: RepoRef, sha: str) -> list[CheckRun]: ...


@runtime_checkable
class CheckLogHost(Protocol):
    """Reading a failed check's log (DF-290): "CI is red" is not actionable, the log is.
    Read only; separate from publishing."""

    def check_log(self, repo: RepoRef, check: CheckRun) -> str | None:
        """The end of the check's log; None when the host keeps no log for it (not a job
        of the host's own CI, or the log has expired)."""
        ...


@runtime_checkable
class ReviewHost(Protocol):
    """Reading and reviewing pull requests (M2-46: skill submissions). Separate from
    `HostingProvider`, so publishing a run needs none of it. Still no merge: a person
    merges, through the host's own branch protection and code owners."""

    def list_pull_requests(self, repo: RepoRef, *, head_prefix: str = "") -> list[PullRequest]: ...

    def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest: ...

    def pull_request_files(self, repo: RepoRef, number: int) -> list[str]:
        """Every path the pull request touches - a renamed file's old path as well as its
        new one. ListingIncomplete when the host lists fewer files than it changes."""
        ...

    def file_at(self, repo: RepoRef, path: str, ref: str) -> bytes | None:
        """The bytes of the file `path` at commit `ref`; None when no file is there."""
        ...

    def common_ancestor(self, repo: RepoRef, base: str, head: str) -> str:
        """The commit a pull request from `head` into `base` is compared with."""
        ...

    def review_pull_request(
        self, repo: RepoRef, number: int, *, approve: bool, body: str, commit: str | None = None
    ) -> None:
        """Approve or request changes; with `commit`, the review is of that commit."""
        ...


@dataclass(frozen=True)
class PullRequestReview:
    """A review of a pull request. `state` is lower case: `changes_requested`,
    `commented`, `approved`, `dismissed` or `pending`."""

    id: int
    author: str  # the host's login
    state: str
    body: str
    submitted_at: datetime | None = None
    commit: str = ""  # the commit it reviewed


@dataclass(frozen=True)
class ReviewComment:
    """An inline review comment. `line` is None when the line is gone from the newest diff
    (an outdated comment); `in_reply_to` names the comment that opened its thread."""

    id: int
    review_id: int | None
    author: str
    body: str
    path: str
    line: int | None
    created_at: datetime | None = None
    in_reply_to: int | None = None
    commit: str = ""


@runtime_checkable
class FeedbackHost(Protocol):
    """Reading what people said about a pull request (M3-11: the learning loop's review
    signals). Read only; separate from publishing and from reviewing. The pull request
    itself is read for its author, whose own comments there are not corrections."""

    def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest: ...

    def pull_request_reviews(self, repo: RepoRef, number: int) -> list[PullRequestReview]: ...

    def pull_request_review_comments(self, repo: RepoRef, number: int) -> list[ReviewComment]: ...


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


#: The generated part of a pull request's description lies between these HTML comments,
#: which hosts do not render; a re-publish replaces only what is between them (DF-296).
PR_BODY_START = "<!-- agentic:run -->"
PR_BODY_END = "<!-- /agentic:run -->"


def _marked(text: str, *, last_end: bool = False) -> tuple[int, int] | None:
    """Where the generated part of `text` starts and ends (markers included), or None.
    With `last_end` (text that is all ours), the part runs to the last end marker."""
    start = text.find(PR_BODY_START)
    after = start + len(PR_BODY_START)
    find = text.rfind if last_end else text.find
    end = find(PR_BODY_END, after) if start >= 0 else -1
    return (start, end + len(PR_BODY_END)) if end >= 0 else None


def splice_body(current: str | None, generated: str) -> str:
    """The description to send when `generated` replaces a pull request's `current` one:
    only the marked part changes, and the text a person wrote before or after it stays.
    A description without both markers (an old one, or one whose markers a person
    deleted) is replaced whole."""
    new, old = _marked(generated, last_end=True), _marked(current or "")
    if new is None or old is None or current is None:
        return generated
    return current[: old[0]] + generated[new[0] : new[1]] + current[old[1] :]


# A URL's userinfo: everything up to the LAST '@' before the host, so a password holding
# an unencoded '@' goes whole (DF-297). A scheme starts only where no scheme character
# precedes it, so a long value is scanned in linear time.
_USERINFO = re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*://)[^/?#\s'\"]+@")


def scrub_url_credentials(text: str) -> str:
    """`text` with the userinfo of every URL in it removed (`https://user:pw@h` -> `https://h`)."""
    return _USERINFO.sub(r"\1", text)


def _shown(text: str) -> str:
    """A remote as it may be shown: its credentials removed."""
    return repr(scrub_url_credentials(text))
