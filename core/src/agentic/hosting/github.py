"""The GitHub adapter (M1-58, spec §16): pull requests and CI checks over the REST API.

Pure Python over httpx2 (D36) - the `gh` CLI is never needed. Works with github.com
(`https://api.github.com`) and GitHub Enterprise Server (`https://<host>/api/v3`, or the
URL the host registry configures). The corporate CA store and proxy variables apply
(`agentic.transport.ssl_context`, spec §15).

The token is kept to the API:

  * it is sent only to the configured API origin: https, or plain http to a loopback
    host (a local fake in tests); redirects are never followed, and a pagination link
    to any other origin is refused;
  * it is never in a repr, and every error message is scrubbed of it, even when the
    server echoes the request back.

CI is read from both of GitHub's mechanisms: check runs (Actions and GitHub Apps) and
commit statuses (Jenkins, many older integrations), mapped into the one vocabulary of
`agentic.hosting.base`. There is no merge call: a person merges.
"""

import ipaddress
from typing import Any
from urllib.parse import quote

import httpx2

from agentic.hosting.base import CheckRun, HostingError, PullRequest, RepoRef, splice_body
from agentic.transport.network import NetworkSettings, ssl_context

GITHUB_COM = "github.com"
GITHUB_COM_API = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_PAGES = 20  # 2000 checks on one commit is far past any real pipeline
#: commit status `state` -> (check `status`, `conclusion`)
_STATUS_STATES = {
    "success": ("completed", "success"),
    "failure": ("completed", "failure"),
    "error": ("completed", "failure"),
    "pending": ("pending", None),
}


class HostingApiError(HostingError):
    """The API answered with an error status (`status`)."""

    def __init__(
        self,
        message: str,
        status: int,
        *,
        transient: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, transient=transient, retry_after=retry_after)
        self.status = status


def api_url_for(host: str, configured: str | None = None) -> str:
    """The REST API root for a GitHub host: the configured one, api.github.com for
    github.com, else GitHub Enterprise Server's `https://<host>/api/v3`."""
    if configured:
        return configured.strip().rstrip("/")
    if host.lower() == GITHUB_COM:
        return GITHUB_COM_API
    return f"https://{host.lower()}/api/v3"


class GitHubHost:
    kind = "github"

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        network: NetworkSettings | None = None,
        timeout: float = 30.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.api_url = _checked_api_url(api_url)
        self._origin = httpx2.URL(self.api_url)
        self._token = token
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "agentic-sdlc",
        }
        if transport is not None:
            self._client = httpx2.Client(
                headers=headers, timeout=timeout, transport=transport, follow_redirects=False
            )
        else:
            settings = network or NetworkSettings()
            self._client = httpx2.Client(
                headers=headers,
                timeout=timeout,
                verify=ssl_context(settings),
                trust_env=settings.trust_env,
                follow_redirects=False,
            )

    def __repr__(self) -> str:
        return f"GitHubHost({self.api_url!r})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubHost":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── the protocol ─────────────────────────────────────────────────────────

    def default_branch(self, repo: RepoRef) -> str:
        data = self._json("GET", self._repo_path(repo))
        return str(data.get("default_branch") or "main")

    def find_pull_request(self, repo: RepoRef, head: str) -> PullRequest | None:
        params = {"state": "open", "head": f"{repo.owner}:{head}", "per_page": "100"}
        rows = self._json("GET", f"{self._repo_path(repo)}/pulls", params=params)
        found = [r for r in rows if isinstance(r, dict) and r.get("head", {}).get("ref") == head]
        return _pull_request(found[0]) if found else None

    def open_pull_request(
        self, repo: RepoRef, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        payload = {"title": title, "head": head, "base": base, "body": body}
        try:
            return _pull_request(self._json("POST", f"{self._repo_path(repo)}/pulls", json=payload))
        except HostingApiError as error:
            if error.status != 422 or "already exists" not in str(error):
                raise
            existing = self.find_pull_request(repo, head)  # opened meanwhile: use that one
            if existing is None:
                raise
            return existing

    def update_pull_request(self, repo: RepoRef, number: int, *, body: str) -> PullRequest:
        """Replace the generated part of the description; what a person wrote around it
        is kept (DF-296). The description is read first, so an edit made meanwhile is
        kept too, short of one landing between the two calls."""
        path = f"{self._repo_path(repo)}/pulls/{int(number)}"
        current = self._json("GET", path)
        said = current.get("body") if isinstance(current, dict) else None
        spliced = splice_body(said if isinstance(said, str) else None, body)
        return _pull_request(self._json("PATCH", path, json={"body": spliced}))

    def checks(self, repo: RepoRef, sha: str) -> list[CheckRun]:
        commit = f"{self._repo_path(repo)}/commits/{quote(sha, safe='')}"
        runs = [
            CheckRun(
                name=str(row.get("name") or "?"),
                status=str(row.get("status") or ""),
                conclusion=row.get("conclusion") or None,
                link=str(row.get("html_url") or ""),
            )
            for page in self._pages(f"{commit}/check-runs")
            for row in page.get("check_runs") or []
            if isinstance(row, dict)
        ]
        for page in self._pages(f"{commit}/status"):
            for row in page.get("statuses") or []:
                if not isinstance(row, dict):
                    continue
                state = str(row.get("state") or "")
                status, conclusion = _STATUS_STATES.get(state, ("completed", state or None))
                runs.append(
                    CheckRun(
                        name=str(row.get("context") or "?"),
                        status=status,
                        conclusion=conclusion,
                        link=str(row.get("target_url") or ""),
                    )
                )
        return runs

    # ── reviews (ReviewHost, M2-46) ──────────────────────────────────────────

    def list_pull_requests(self, repo: RepoRef, *, head_prefix: str = "") -> list[PullRequest]:
        """The open pull requests whose branch starts with `head_prefix` (the newest 100)."""
        params = {"state": "open", "per_page": "100", "sort": "created", "direction": "desc"}
        rows = self._json("GET", f"{self._repo_path(repo)}/pulls", params=params)
        found = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("head", {}).get("ref", "")).startswith(
                head_prefix
            ):
                found.append(_pull_request(row))
        return found

    def get_pull_request(self, repo: RepoRef, number: int) -> PullRequest:
        return _pull_request(self._json("GET", f"{self._repo_path(repo)}/pulls/{int(number)}"))

    def pull_request_files(self, repo: RepoRef, number: int) -> list[str]:
        """The paths the pull request changes (its first 3000, GitHub's own limit)."""
        path = f"{self._repo_path(repo)}/pulls/{int(number)}/files"
        found: list[str] = []
        for page in range(1, 31):
            rows = self._json("GET", path, params={"per_page": "100", "page": str(page)})
            names = [str(r.get("filename")) for r in rows if isinstance(r, dict)]
            found += [name for name in names if name]
            if not isinstance(rows, list) or len(rows) < 100:
                break
        return found

    def review_pull_request(self, repo: RepoRef, number: int, *, approve: bool, body: str) -> None:
        """Approve, or request changes with `body` saying why. A host refuses a review of
        one's own pull request; that refusal is a HostingApiError like any other."""
        event = "APPROVE" if approve else "REQUEST_CHANGES"
        path = f"{self._repo_path(repo)}/pulls/{int(number)}/reviews"
        self._json("POST", path, json={"event": event, "body": body})

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _repo_path(self, repo: RepoRef) -> str:
        if "/" in repo.owner or not repo.owner or not repo.name:
            raise HostingError(f"{repo.slug} is not a GitHub owner/name repository")
        return f"/repos/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}"

    def _pages(self, path: str) -> list[dict]:
        """Every page of a paginated GET, following `Link: next` on the API origin only."""
        pages: list[dict] = []
        url: str | None = self.api_url + path
        params: dict | None = {"per_page": "100"}
        for _ in range(MAX_PAGES):
            response = self._send("GET", url, params=params)
            data = _body(response)
            pages.append(data if isinstance(data, dict) else {})
            url = response.links.get("next", {}).get("url")
            if not url:
                break
            params = None  # the next link carries its own query
        return pages

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return _body(self._send(method, self.api_url + path, **kwargs))

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        target = httpx2.URL(url)
        if not self._same_origin(target):
            raise HostingError(
                f"GitHub API: refused to follow a link to another origin ({target.host})"
            )
        where = f"{method} {target.path}"
        try:
            response = self._client.request(method, target, **kwargs)
        except httpx2.HTTPError as error:
            reason = f"{type(error).__name__}: {error}"
            message = f"GitHub API {where}: could not reach {self._origin.host} ({reason})"
            raise HostingError(self._scrub(message), transient=True) from None
        if response.status_code >= 300:
            raise HostingApiError(
                self._scrub(_explain(where, response)),
                response.status_code,
                transient=_transient(response),
                retry_after=_retry_after(response),
            )
        return response

    def _same_origin(self, url: httpx2.URL) -> bool:
        origin = self._origin
        return (
            url.scheme == origin.scheme
            and url.host == origin.host
            and url.port == origin.port
            and url.path.startswith(origin.path.rstrip("/") + "/")
        )

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "<redacted>") if self._token else text


def _checked_api_url(api_url: str) -> str:
    url = (api_url or "").strip().rstrip("/")
    try:
        parsed = httpx2.URL(url)
    except Exception:  # noqa: BLE001 - any unparsable URL is the same refusal
        parsed = None
    if parsed is not None and parsed.scheme == "https" and parsed.host:
        return url
    if parsed is not None and parsed.scheme == "http" and _loopback(parsed.host):
        return url
    raise HostingError(
        f"the GitHub API URL must be https (plain http only to this machine): {url!r}"
    )


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _body(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise HostingError(
            f"GitHub API {response.request.method} {response.request.url.path}: "
            f"the answer is not JSON (HTTP {response.status_code})"
        ) from None


def _rate_limited(response: httpx2.Response) -> bool:
    """429, or GitHub's 403 rate limits: the primary one (no requests left) and the
    secondary one (a `Retry-After` on a 403)."""
    status, headers = response.status_code, response.headers
    return status == 429 or (
        status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
    )


def _transient(response: httpx2.Response) -> bool:
    return response.status_code >= 500 or _rate_limited(response)


def _retry_after(response: httpx2.Response) -> float | None:
    """`Retry-After` in seconds (GitHub sends seconds; an HTTP date is not used)."""
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _explain(where: str, response: httpx2.Response) -> str:
    status = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    said = str(data.get("message") or "").strip()
    details = [
        str(e.get("message") or e.get("code") or "")
        for e in data.get("errors") or []
        if isinstance(e, dict)
    ]
    said = "; ".join(p for p in [said, *details] if p)
    if status == 401:
        meaning = "the token was rejected (expired, revoked or for another host)"
    elif _rate_limited(response):
        meaning = "rate limited by the API; try again later"
    elif status == 403:
        meaning = "the token lacks permission for this repository (needs pull request write)"
    elif status == 404:
        meaning = "not found: the repository does not exist or the token cannot see it"
    elif status in (301, 308):  # the API's answer for a renamed or transferred repository
        meaning = (
            "the repository was renamed or moved: point origin at its new address with "
            "`git remote set-url origin <new url>` (the redirect is not followed)"
        )
    elif 300 <= status < 400:
        meaning = "the API answered with a redirect, which is not followed"
    else:
        meaning = "the API refused the request"
    return f"GitHub API {where}: HTTP {status} - {meaning}" + (f" ({said})" if said else "")


def _pull_request(data: Any) -> PullRequest:
    if not isinstance(data, dict) or "number" not in data:
        raise HostingError("GitHub API: the answer is not a pull request")
    head, base = data.get("head") or {}, data.get("base") or {}
    return PullRequest(
        number=int(data["number"]),
        url=str(data.get("html_url") or ""),
        head=str(head.get("ref") or ""),
        base=str(base.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        state=str(data.get("state") or "open"),
        # null is an empty description; no `body` key at all: not known (None)
        body=str(data["body"] or "") if "body" in data else None,
        title=str(data.get("title") or ""),
        author=str((data.get("user") or {}).get("login") or ""),
        merged=bool(data.get("merged") or data.get("merged_at")),
    )
