"""The control-plane client (SP15 Task 10): login, the tenant's policy bundle, permission
checks, repo registration and run event ingest.

The CLI never names a tenant: the server derives it from the token. Every call carries
`Authorization: Bearer <token>`, `User-Agent: agentic-cli/<version>` and the CLI version
header. TLS goes through the OS certificate store (truststore) and the proxy variables
are honoured, as for the gateway. A problem document from the server becomes a `CliError`
carrying the server's hint; a token or a URL's credentials never appear in a message.

The token lives in the OS keyring (`control_plane_token`, or `AGENTIC_CONTROL_PLANE_TOKEN`
in CI). `agentic login --for control-plane` records the URL, tenant and user it verified in
`~/.agentic/control-plane.json` (no secret); a keyring token is only ever sent to that URL
(ruling R18), so a changed URL setting cannot send it to another server."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from agentic.auth import CredentialError, Credentials
from agentic.hosting.base import scrub_url_credentials
from agentic.orgapi import (
    API_PREFIX,
    CLI_VERSION_HEADER,
    ActivePolicy,
    CheckResult,
    IngestResult,
    KillState,
    KillSwitchInfo,
    Me,
    RepoInfo,
    RunPreflight,
    RunSummary,
)
from agentic.security.pii import PiiDetector
from agentic.security.publish_scan import PublishBlocked, PublishGuard
from agentic.security.secret_scan import SecretScanner
from agentic.telemetry.sinks import SinkBehind
from agentic.transport.network import NetworkSettings, ssl_context
from agentic_cli import __version__
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

CONTROL_PLANE_TOKEN = "control_plane_token"
URL_ENV = "AGENTIC_CONTROL_PLANE_URL"
LOGIN = "agentic login --for control-plane"
LOGIN_HINT = f"run `{LOGIN}`"
LOGIN_RECORD = "control-plane.json"
#: Seconds to connect, and to wait for an answer (a short default: the CLI waits on it).
TIMEOUT = httpx2.Timeout(15.0, connect=5.0)
#: The kill-switch poll waits less: a run asks before every step and turn (M2-19).
KILL_STATE_TIMEOUT = httpx2.Timeout(5.0, connect=3.0)
#: Serialised entries per ingest request: about half the control plane's default body
#: cap (`max_body_bytes`, 1 MiB), leaving room for the envelope (review F6).
MAX_BATCH_BYTES = 512 * 1024


class ControlPlaneError(CliError):
    """A control-plane call failed."""


class ControlPlaneUnavailable(ControlPlaneError):
    """The control plane could not be reached (exit 1): a network or server problem."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message, ExitCode.ERROR, hint=hint)


class ControlPlaneRefused(ControlPlaneError):
    """The control plane refused (401/403/404/409/422/426/429; exit 2), or the CLI refused
    to use what it answered (a bundle whose digest does not match)."""

    def __init__(
        self,
        message: str,
        hint: str | None = None,
        *,
        code_name: str = "",
        status: int = 0,
        head: tuple[int, str] | None = None,
        problems: list[str] | None = None,
    ) -> None:
        super().__init__(message, ExitCode.REFUSED, hint=hint)
        self.code_name = code_name  # the problem's machine `code` (`ingest.gap`, ...)
        self.status = status
        self.head = head  # the server's chain head, on an ingest gap or fork
        self.problems = problems or []  # a broken chain's problems


class ChainRejected(ControlPlaneRefused):
    """An ingest batch was refused: `problems` (a broken chain) or `head` (a gap or a fork:
    the server's last seq and hash)."""

    def __init__(
        self,
        message: str,
        *,
        code_name: str,
        problems: list[str],
        head: tuple[int, str] | None,
    ) -> None:
        super().__init__(message, code_name=code_name, status=409, head=head, problems=problems)


def control_plane_url(env: Mapping[str, str], settings: Mapping[str, Any]) -> str | None:
    """The control plane's URL: `AGENTIC_CONTROL_PLANE_URL`, else the user config's
    `control_plane_url`; None when neither is set."""
    found = env.get(URL_ENV, "").strip() or str(settings.get("control_plane_url") or "")
    return found.strip().rstrip("/") or None


def checked_base(url: str) -> str:
    """`url` without a trailing slash; https, or plain http to a loopback address only (a
    token never crosses a network in clear)."""
    base = url.strip().rstrip("/")
    parsed = httpx2.URL(base)
    if parsed.scheme == "https" and parsed.host:
        return base
    if parsed.scheme == "http" and _loopback(parsed.host):
        return base
    raise ControlPlaneRefused(
        f"the control plane URL must use https:// ({scrub_url_credentials(base)})",
        hint=f"fix control_plane_url in ~/.agentic/config.yaml or {URL_ENV}",
    )


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: httpx2.Timeout = TIMEOUT,
    ) -> None:
        self.base_url = checked_base(base_url)
        self._shown = scrub_url_credentials(self.base_url)
        self._token = token
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"agentic-cli/{__version__}",
            CLI_VERSION_HEADER: __version__,
            "Accept": "application/json, application/problem+json",
        }
        options: dict[str, Any] = {
            "base_url": self.base_url,
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": False,
        }
        if transport is not None:
            options["transport"] = transport
        else:
            settings = NetworkSettings()
            options["verify"] = ssl_context(settings)
            options["trust_env"] = settings.trust_env
        self._http = httpx2.Client(**options)

    def __repr__(self) -> str:  # never the token
        return f"ControlPlaneClient({self._shown!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ControlPlaneClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── calls ────────────────────────────────────────────────────────────────

    def me(self) -> Me:
        return Me.model_validate(self._json("GET", "/me"))

    def active_policy(self) -> ActivePolicy:
        return ActivePolicy.model_validate(self._json("GET", "/policy/active"))

    def bundle(self, active: ActivePolicy) -> bytes:
        """The active version's bundle, refused unless its sha256 is `active.digest`."""
        response = self._send("GET", active.bundle_path.removeprefix(API_PREFIX))
        data = response.content
        if "sha256:" + hashlib.sha256(data).hexdigest() != active.digest:
            raise ControlPlaneRefused(
                f"the policy bundle from {self._shown} does not match its digest; "
                "nothing was cached",
                hint="run `agentic init` again; if it repeats, tell your org admin",
            )
        return data

    def check(self, permission: str, *, team: str | None = None, repo: str | None = None):
        body = {"permission": permission, "team": team, "repo": repo}
        found = self._json("POST", "/rbac/check", {k: v for k, v in body.items() if v})
        return CheckResult.model_validate(found)

    def register_repo(self, remote: str, team: str) -> RepoInfo:
        return RepoInfo.model_validate(
            self._json("POST", "/repos", {"remote": remote, "team": team})
        )

    def ingest(self, run_id: str, entries: Sequence[Mapping[str, object]]) -> IngestResult:
        """Send a batch of the run's chained events. `ChainRejected` on a gap, a fork or a
        broken chain (the server's head or problems attached)."""
        try:
            found = self._json("POST", f"/runs/{run_id}/events", {"entries": list(entries)})
        except ControlPlaneRefused as error:
            if error.code_name.startswith("ingest.") and error.code_name != "ingest.too_large":
                raise ChainRejected(
                    str(error), code_name=error.code_name, problems=error.problems, head=error.head
                ) from None
            raise
        return IngestResult.model_validate(found)

    def run(self, run_id: str) -> RunSummary:
        """The run as the control plane holds it (its chain head included)."""
        return RunSummary.model_validate(self._json("GET", f"/runs/{run_id}"))

    def register_run(self, body: Mapping[str, object]) -> RunPreflight:
        """The org checks before a run starts (M2-21); registers it when they all pass and
        `dry_run` is not set. An old CLI is refused (426, exit 2, the upgrade hint)."""
        return RunPreflight.model_validate(self._json("POST", "/runs", dict(body)))

    def kill_state(self, *, team: str | None, repo: str | None, pipeline: str | None) -> KillState:
        """The switches that may apply to a run of this team, repo and pipeline (M2-19)."""
        params = {"team": team, "repo": repo, "pipeline": pipeline}
        found = self._json(
            "GET",
            "/killswitch/state",
            params={k: v for k, v in params.items() if v},
            timeout=KILL_STATE_TIMEOUT,
        )
        return KillState.model_validate(found)

    def set_switch(self, body: Mapping[str, object]) -> KillSwitchInfo:
        return KillSwitchInfo.model_validate(self._json("POST", "/killswitch", dict(body)))

    def clear_switch(self, switch_id: str, reason: str | None = None) -> KillSwitchInfo:
        params = {"reason": reason} if reason else None
        found = self._json("DELETE", f"/killswitch/{switch_id}", params=params)
        return KillSwitchInfo.model_validate(found)

    def switches(self, *, include_cleared: bool = False) -> list[KillSwitchInfo]:
        params = {"include_cleared": "true"} if include_cleared else None
        found = self._json("GET", "/killswitch", params=params)
        return [KillSwitchInfo.model_validate(item) for item in found]

    def run_url(self, run_id: str) -> str:
        """Where a person reads the run (ND-11: the JSON run summary until a web UI)."""
        return f"{self._shown}{API_PREFIX}/runs/{run_id}"

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _json(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: Mapping[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
    ) -> Any:
        response = self._send(method, path, body, params=params, timeout=timeout)
        try:
            return response.json()
        except ValueError:
            raise ControlPlaneUnavailable(
                f"{self._shown} answered something that is not JSON ({response.status_code})"
            ) from None

    def _send(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: Mapping[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
    ) -> httpx2.Response:
        options: dict[str, Any] = {"json": body}
        if params:
            options["params"] = dict(params)
        if timeout is not None:
            options["timeout"] = timeout
        try:
            response = self._http.request(method, f"{API_PREFIX}{path}", **options)
        except httpx2.HTTPError as error:
            raise ControlPlaneUnavailable(
                f"cannot reach the control plane at {self._shown} ({type(error).__name__})",
                hint="check the network or VPN, and control_plane_url",
            ) from None
        if response.status_code < 400:
            return response
        raise self._problem(response)

    def _problem(self, response: httpx2.Response) -> ControlPlaneError:
        try:
            problem = response.json()
        except ValueError:
            problem = {}
        if not isinstance(problem, dict):
            problem = {}
        detail = str(problem.get("detail") or f"HTTP {response.status_code}")
        code = str(problem.get("code") or "")
        hint = problem.get("hint")
        status = response.status_code
        if status >= 500:
            return ControlPlaneUnavailable(f"the control plane failed: {detail}", hint=hint)
        if status == 401 and not hint:
            hint = LOGIN_HINT
        head = problem.get("head")
        found = problem.get("problems")
        return ControlPlaneRefused(
            f"the control plane refused: {detail}",
            hint,
            code_name=code,
            status=status,
            head=(int(head.get("seq", 0)), str(head.get("hash", "")))
            if isinstance(head, dict)
            else None,
            problems=[str(p) for p in found] if isinstance(found, list) else None,
        )


# ── the run event sink (M2-20) ───────────────────────────────────────────────


class ControlPlaneSink:
    """An `agentic.telemetry.sinks.EventSink` that ingests run ledger entries into the
    control plane, in batches. Any refusal raises, so `forward` keeps its cursor and the
    entries stay queued in the ledger; a control plane whose head is behind the batch
    raises `SinkBehind`, and `forward` catches it up from that head (R21). Replays are
    no-ops on the server (duplicates), so delivery is at least once and never twice.

    With a `guard` (the publish scan, M2-49) every entry is checked before anything is
    sent: the clean entries before one holding a secret or personal data are sent, then
    `PublishBlocked` is raised (audited), so it and what follows stay in the ledger."""

    name = "control-plane"

    def __init__(
        self,
        client: ControlPlaneClient,
        batch: int = 200,
        max_bytes: int = MAX_BATCH_BYTES,
        *,
        guard: PublishGuard | None = None,
    ) -> None:
        self.client = client
        self.batch = batch
        self.max_bytes = max_bytes
        self.guard = guard

    def deliver(self, run_id: str, entries: Sequence[Mapping[str, object]]) -> None:
        """Send `entries` in batches of at most `batch` entries and about `max_bytes`;
        a 413 (the server's body or entry cap is lower) halves the batch and retries,
        so a large offline backlog is never wedged (review F6). Under the publish scan the
        clean entries before a held-back one are sent, then `PublishBlocked` is raised
        with `delivered` set, so the control plane has the run up to that entry."""
        blocked: PublishBlocked | None = None
        if self.guard is not None:
            try:
                self.guard.check(run_id, entries)
            except PublishBlocked as error:
                blocked, entries = error, entries[: error.index]
        self._send(run_id, entries)
        if blocked is not None:
            blocked.delivered = len(entries)
            raise blocked

    def _send(self, run_id: str, entries: Sequence[Mapping[str, object]]) -> None:
        start, limit = 0, self.batch
        while start < len(entries):
            chunk = self._chunk(entries, start, limit)
            try:
                self.client.ingest(run_id, chunk)
            except ChainRejected as error:
                first = chunk[0].get("seq")
                head = error.head
                if (
                    error.code_name == "ingest.gap"
                    and head is not None
                    and isinstance(first, int)
                    and head[0] < first - 1
                ):
                    raise SinkBehind(*head) from None
                raise
            except ControlPlaneRefused as error:
                if error.status == 413 and len(chunk) > 1:
                    limit = len(chunk) // 2
                    continue
                raise
            start += len(chunk)

    def _chunk(
        self, entries: Sequence[Mapping[str, object]], start: int, limit: int
    ) -> Sequence[Mapping[str, object]]:
        """Entries from `start`: at most `limit`, and at least one, within `max_bytes`."""
        end, size = start, 0
        while end < len(entries) and end - start < limit:
            size += len(json.dumps(entries[end], separators=(",", ":"))) + 1
            if end > start and size > self.max_bytes:
                break
            end += 1
        return entries[start:end]


# ── which client, from where ────────────────────────────────────────────────


def login_record(home: Path) -> dict | None:
    """What `agentic login --for control-plane` verified last (no secret), or None."""
    try:
        data = json.loads((home / LOGIN_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_login(home: Path, url: str, tenant: str, user: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    record = {
        "url": url.rstrip("/"),
        "tenant": tenant,
        "user": user,
        "verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    staged = home / f".{LOGIN_RECORD}.tmp-{os.getpid()}"
    staged.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(staged, home / LOGIN_RECORD)


def client_for(
    env: Mapping[str, str],
    settings: Mapping[str, Any],
    home: Path,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> ControlPlaneClient | None:
    """The client for the configured control plane, or None when none is configured.
    Refused without a token, or when a keyring token was verified for another URL (R18)."""
    url = control_plane_url(env, settings)
    if url is None:
        return None
    try:
        found = Credentials(environ=env).find(CONTROL_PLANE_TOKEN, login=LOGIN)
    except CredentialError as error:
        raise ControlPlaneRefused(str(error), hint=LOGIN_HINT) from None
    if found is None:
        raise ControlPlaneRefused(
            f"not signed in to the control plane at {scrub_url_credentials(url)}",
            hint=LOGIN_HINT,
        )
    if found.source == "keyring":
        record = login_record(home) or {}
        if str(record.get("url", "")).rstrip("/") != url:
            raise ControlPlaneRefused(
                f"the stored control-plane token was verified for another URL, not "
                f"{scrub_url_credentials(url)}; it is not sent there",
                hint=f"{LOGIN_HINT} to sign in to this control plane",
            )
    return ControlPlaneClient(url, found.value, transport=transport or make_transport())


def publish_guard(policy: Callable[[], Any]) -> PublishGuard:
    """The publish scan for what goes to the control plane (M2-49): org policy's, or -
    when the policy cannot be read here (`agentic sync` in a repository not yet set up) -
    the defaults, which mask every secret and personal-data kind. Never no scan."""
    try:
        return PublishGuard.from_policy(policy())
    except CliError:
        return PublishGuard(SecretScanner(), PiiDetector())


def make_transport() -> httpx2.BaseTransport | None:
    """The transport for real calls (None: httpx2's own, with TLS and proxies). Tests
    replace this function to talk to a fake control plane."""
    return None
