"""The Jira adapter (M2-50, spec §16): read an issue, comment on it, move it on.

Pure Python over httpx2 (D36), for Jira Cloud (`https://<site>.atlassian.net`) and Jira
Data Center or Server (`https://jira.acme.example`, with or without a context path such
as `/jira`). Both serve REST API v2, the default here; `api_version="3"` uses Cloud's v3,
where an issue's description arrives in Atlassian Document Format (the connector adapter
reads it as text) and a comment is sent as one. The corporate CA store and proxy
variables apply (`agentic.transport.ssl_context`, spec §15).

Authentication: with `user`, HTTP Basic `user:token` - on Cloud the account's email and
an API token (required there); on Data Center a user name and password or token. Without
`user`, a Bearer token: a Data Center personal access token.

The token is kept to the site:

  * it is sent only to the configured origin: https, or plain http to a loopback host (a
    local fake in tests); redirects are never followed;
  * it is never in a repr, and every error message is scrubbed of it (and of the Basic
    header that carries it), even when the server echoes the request back.

Nothing here decides what a run does with an issue: mapping it to a request is the
connector adapter's job (`sdlc_commons.connectors`), and the CLI decides when to comment.
"""

import base64
import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx2

from agentic.transport.network import NetworkSettings, ssl_context

API_VERSIONS = ("2", "3")
CLOUD_SUFFIXES = (".atlassian.net", ".jira.com")
_KEY = re.compile(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*")


class JiraError(Exception):
    """A Jira call that failed, or a setting that cannot work; the message says why.
    `status` is the HTTP status when Jira answered; `transient` errors may pass if tried
    again later (`retry_after` seconds, when Jira said)."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        *,
        transient: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status, self.transient, self.retry_after = status, transient, retry_after


@dataclass(frozen=True)
class JiraTransition:
    id: str
    name: str  # the transition's own name ("Start work")
    to: str  # the status it leads to ("In Progress")


def issue_key(text: str) -> str:
    """`text` as an issue key (`PROJ-123`, upper-cased); a JiraError for anything else, so
    no other text ever becomes part of a request path."""
    key = str(text or "").strip().upper()
    if not _KEY.fullmatch(key):
        raise JiraError(f"{text!r} is not a Jira issue key (a project key and a number: PROJ-123)")
    return key


def is_cloud(url: str) -> bool:
    """Whether `url` is a Jira Cloud site (which needs the account email as `user`)."""
    try:
        host = httpx2.URL(str(url).strip()).host.lower()
    except Exception:  # noqa: BLE001 - an unparsable URL is not a Cloud site
        return False
    return host.endswith(CLOUD_SUFFIXES)


class JiraClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        user: str | None = None,
        api_version: str = "2",
        network: NetworkSettings | None = None,
        timeout: float = 30.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.base_url = _checked_base_url(base_url)
        if str(api_version) not in API_VERSIONS:
            raise JiraError(
                f"api_version must be one of {', '.join(API_VERSIONS)}, not {api_version!r}"
            )
        if is_cloud(self.base_url) and not (user or "").strip():
            raise JiraError(
                "a Jira Cloud site needs the account's email as the user (with an API token); "
                "set jira.user"
            )
        self.api_version = str(api_version)
        self._origin = httpx2.URL(self.base_url)
        self._api = f"{self.base_url}/rest/api/{self.api_version}"
        if user and user.strip():
            pair = base64.b64encode(f"{user.strip()}:{token}".encode()).decode("ascii")
            authorization = f"Basic {pair}"
        else:
            authorization = f"Bearer {token}"
        self._secrets = [s for s in (authorization.split(" ", 1)[1], token) if s]
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
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
        return f"JiraClient({self.base_url!r}, api_version={self.api_version!r})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── the calls ────────────────────────────────────────────────────────────

    def issue(self, key: str) -> dict:
        """The issue as Jira returns it (`key`, `self`, `fields`...)."""
        data = self._json("GET", self._issue_path(key))
        if not isinstance(data, dict) or "fields" not in data:
            raise JiraError(f"Jira GET {self._issue_path(key)}: the answer is not an issue")
        return data

    def comment(self, key: str, text: str) -> str:
        """Add a comment; its id. v2 takes plain text (Jira links bare URLs), v3 an ADF
        document with one paragraph per non-empty line."""
        body: Any = text if self.api_version == "2" else _adf(text)
        data = self._json("POST", f"{self._issue_path(key)}/comment", json={"body": body})
        return str(data.get("id") or "") if isinstance(data, dict) else ""

    def transitions(self, key: str) -> list[JiraTransition]:
        """The transitions the token's user may make on the issue now."""
        data = self._json("GET", f"{self._issue_path(key)}/transitions")
        rows = data.get("transitions") if isinstance(data, dict) else None
        return [
            JiraTransition(
                id=str(row.get("id") or ""),
                name=str(row.get("name") or ""),
                to=str((row.get("to") or {}).get("name") or ""),
            )
            for row in rows or []
            if isinstance(row, dict) and row.get("id")
        ]

    def transition(self, key: str, name: str) -> JiraTransition:
        """Make the transition called `name`, or leading to the status `name` (either,
        any case). A JiraError naming the available ones when there is none."""
        wanted = str(name or "").strip().casefold()
        available = self.transitions(key)
        found = next((t for t in available if t.name.casefold() == wanted), None)
        found = found or next((t for t in available if t.to.casefold() == wanted), None)
        if found is None:
            shown = ", ".join(t.name if t.name == t.to else f"{t.name} ({t.to})" for t in available)
            raise JiraError(
                f"{issue_key(key)} has no transition {name!r} now; available: {shown or 'none'}"
            )
        self._send(
            "POST", f"{self._issue_path(key)}/transitions", json={"transition": {"id": found.id}}
        )
        return found

    def browse_url(self, key: str) -> str:
        """Where a person opens the issue."""
        return f"{self.base_url}/browse/{issue_key(key)}"

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _issue_path(self, key: str) -> str:
        return f"/issue/{quote(issue_key(key), safe='')}"

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._send(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise JiraError(
                f"Jira {method} {self._shown(path)}: the answer is not JSON (HTTP "
                f"{response.status_code}) - a single sign-on page in front of Jira? check "
                "jira.url and the token",
                response.status_code,
            ) from None

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        target = httpx2.URL(self._api + path)
        where = f"{method} {self._shown(path)}"
        try:
            response = self._client.request(method, target, **kwargs)
        except httpx2.HTTPError as error:
            reason = f"{type(error).__name__}: {error}"
            message = f"Jira {where}: could not reach {self._origin.host} ({reason})"
            raise JiraError(self._scrub(message), transient=True) from None
        if response.status_code >= 300:
            status = response.status_code
            raise JiraError(
                self._scrub(_explain(where, response)),
                status,
                transient=status >= 500 or status == 429,
                retry_after=_retry_after(response),
            )
        return response

    def _shown(self, path: str) -> str:
        return httpx2.URL(self._api + path).path

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text


def _checked_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    # a user name or password in the URL would replace the token and leak into reprs and
    # errors, so it is refused and never echoed
    if "@" in (url.partition("://")[2] or url).split("/", 1)[0]:
        raise JiraError(
            "the Jira URL may not carry a user name or password: give the site URL alone, "
            "with the account in jira.user and the token in the credential store"
        )
    try:
        parsed = httpx2.URL(url)
    except Exception:  # noqa: BLE001 - any unparsable URL is the same refusal
        parsed = None
    if parsed is not None and parsed.host and not parsed.query:
        if parsed.scheme == "https" or (parsed.scheme == "http" and _loopback(parsed.host)):
            return url
    raise JiraError(f"the Jira URL must be https (plain http only to this machine): {url!r}")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _adf(text: str) -> dict:
    lines = [line for line in str(text).splitlines() if line.strip()]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines
        ],
    }


def _retry_after(response: httpx2.Response) -> float | None:
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
    said = [str(m) for m in data.get("errorMessages") or [] if str(m).strip()]
    errors = data.get("errors") if isinstance(data.get("errors"), dict) else {}
    said += [f"{field}: {message}" for field, message in errors.items()]
    if not data and response.text.strip() and not response.text.lstrip().startswith("<"):
        said.append(response.text.strip()[:200])
    if status == 401:
        meaning = (
            "the token was rejected (expired, revoked, for another site, or the user does "
            "not match it)"
        )
    elif status == 403:
        meaning = (
            "the token's user may not do this (reading needs Browse projects, commenting Add "
            "comments, a transition Transition issues)"
        )
    elif status == 404:
        meaning = "not found: the issue does not exist or the token's user cannot see it"
    elif status == 429:
        meaning = "rate limited by Jira; try again later"
    elif 300 <= status < 400:
        meaning = (
            "Jira answered with a redirect, which is not followed (check jira.url: a Cloud "
            "site is https://<site>.atlassian.net)"
        )
    else:
        meaning = "Jira refused the request"
    return f"Jira {where}: HTTP {status} - {meaning}" + (f" ({'; '.join(said)})" if said else "")
