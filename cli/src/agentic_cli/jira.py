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

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic.auth import CredentialError, Credentials, env_var
from agentic.definitions.pipeline import PipelineDefinition
from agentic.engine.state import RunState
from agentic.integrations.jira import API_VERSIONS, JiraClient, JiraError, is_cloud, issue_key
from agentic.security.untrusted import untrusted
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from sdlc_commons.connectors import ConnectorError, ExternalItem, item_for, mapping_for

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
    authority = (url.partition("://")[2] or url).split("/", 1)[0]
    if "@" in authority:  # never echoed: it holds a password
        raise CliError(
            "the Jira site URL may not carry a user name or password",
            ExitCode.INVALID,
            hint="give the site URL alone in jira.url (or "
            f"{URL_ENV}); the account goes in jira.user, the token in `{LOGIN}`",
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


# ── a run from an issue ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class JiraRequest:
    """One issue, read and mapped: the run's request text and where the issue lives."""

    key: str
    item: ExternalItem
    text: str  # the input value: a header line, then the specification as untrusted data
    url: str  # where a person opens the issue


def jira_input(pipeline: PipelineDefinition) -> str:
    """The pipeline input an issue fills: the one whose `from:` lists `jira`, or `request`
    for a pipeline that declares no inputs. A usage error otherwise."""
    if not pipeline.inputs:
        return "request"
    takes = [
        name
        for name, spec in pipeline.inputs.items()
        if JIRA in [str(s) for s in _sources(spec.get("from"))]
    ]
    if len(takes) == 1:
        return takes[0]
    why = "takes no Jira input" if not takes else f"has several Jira inputs ({', '.join(takes)})"
    raise CliError(
        f"pipeline {pipeline.name} {why}",
        ExitCode.USAGE,
        hint="a pipeline reads an issue into the input whose `from:` lists jira, e.g. "
        "inputs: {request: {from: [text, jira]}}; or pass --input instead",
    )


def read_request(
    client: JiraClient, repo: Path, key: str, *, token_hint: str | None = None
) -> JiraRequest:
    """The issue `key`, mapped by the repo's connector (Zyroflo's `.sdlc/connectors/jira.json`,
    else the shipped one) into the run's request."""
    try:
        key = issue_key(key)
    except JiraError as error:
        raise CliError(str(error), ExitCode.USAGE) from error
    try:
        payload = client.issue(key)
    except JiraError as error:
        raise jira_failure(error, token_hint) from error
    try:
        item = item_for(JIRA, mapping_for(repo, JIRA), payload)
    except ConnectorError as error:
        raise CliError(
            f"{key} could not be read through the Jira connector: {error}",
            ExitCode.INVALID,
            hint="check .sdlc/connectors/jira.json (the dotted paths of the title, body, id "
            "and link in your Jira's issue JSON), or remove it to use the shipped mapping",
        ) from error
    return JiraRequest(key, item, request_text(item, key), client.browse_url(key))


def request_text(item: ExternalItem, key: str) -> str:
    """The run's request: a header naming the issue, then Zyroflo's specification inside an
    `<untrusted-data>` fence - ticket text is data, never instructions (M1-42)."""
    return f"Jira issue {key}\n\n" + untrusted(item.spec, source=f"jira {key}")


def jira_failure(error: JiraError, token: str | None = None) -> CliError:
    """A failed read as a CliError: refused for a rejected token (`token`: how to replace
    it), invalid for an issue that is not there, an error otherwise."""
    if error.status in (401, 403):
        hint = token or f"set {env_var(JIRA_TOKEN)}, or run `{LOGIN}`"
        return CliError(str(error), ExitCode.REFUSED, hint=hint)
    if error.status == 404:
        return CliError(str(error), ExitCode.INVALID, hint="check the issue key and the site")
    return CliError(str(error), ExitCode.ERROR)


def start_transition(
    client: JiraClient, request: JiraRequest, name: str
) -> tuple[dict | None, str | None]:
    """Move the issue on as the run starts: `({name, to}, None)`, or `(None, warning)`."""
    try:
        done = client.transition(request.key, name)
    except JiraError as error:
        return None, f"could not transition {request.key} to {name!r}: {error}"
    return {"name": done.name, "to": done.to}, None


def run_comment(state: RunState, *, link: str | None, cost_usd: str) -> str:
    """What the issue is told about the run: its id, status, cost and where to see it."""
    status = state.status.replace("_", " ")
    if state.pending is not None:
        status = f"{status} ({state.pending.kind} {state.pending.id})"
    lines = [
        f"agentic run {state.run_id} ({state.pipeline}) was started from this issue by "
        f"{state.starter}.",
        f"Status: {status}. Cost so far: ${cost_usd}.",
    ]
    if link:
        lines.append(f"Run: {link}")
    lines.append(f"Details: `agentic status {state.run_id}` in the starter's checkout.")
    return "\n".join(lines)


def report_run(
    client: JiraClient,
    request: JiraRequest,
    state: RunState,
    folder: Path,
    *,
    text: str,
    transition: dict | None,
    warnings: list[str],
) -> dict:
    """Comment on the issue, and write `jira.json` in the run folder (no secret). Returns
    that record, its warnings included: a failed comment never fails the run."""
    warnings = list(warnings)
    comment_id = None
    try:
        comment_id = client.comment(request.key, text) or None
    except JiraError as error:
        warnings.append(f"could not comment on {request.key}: {error}")
    record = {
        "issue": request.key,
        "url": request.url,
        "identity": request.item.identity,
        "comment_id": comment_id,
        "transition": transition,
        "warnings": warnings,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "jira.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    return record


class IssueRun:
    """One run started from an issue: read before the run, moved on as it starts, told how
    it went when the command returns. Only the read can stop the run from starting."""

    def __init__(self, ws, client: JiraClient, settings: JiraSettings, request: JiraRequest):
        self.ws, self.client, self.settings, self.request = ws, client, settings, request
        self.transition: dict | None = None
        self.warnings: list[str] = []

    @classmethod
    def open(cls, ws, key: str, credentials: Credentials | None = None) -> "IssueRun":
        settings = jira_settings(ws.settings, ws.env)
        client = connect_jira(ws, credentials)
        try:
            request = read_request(client, ws.repo, key, token_hint=token_hint(settings))
        except BaseException:
            client.close()
            raise
        return cls(ws, client, settings, request)

    def start(self, out, transition: str | None) -> None:
        """The run has started: make the transition asked for (or configured), if any."""
        name = transition or self.settings.transition
        if not name:
            return
        self.transition, warning = start_transition(self.client, self.request, name)
        if warning:
            self.warnings.append(warning)
            out.warn(warning)

    def report(self, out, state: RunState, folder: Path, redact) -> dict:
        """Comment the run's id, status, cost and link on the issue; record it in jira.json."""
        # here, not at the top: `agentic login` imports this module and needs neither
        from agentic_cli.driving import spent
        from agentic_cli.publishing import run_link

        text = redact(
            run_comment(state, link=run_link(self.ws, state.run_id), cost_usd=str(spent(state)))
        )
        try:
            record = report_run(
                self.client,
                self.request,
                state,
                folder,
                text=text,
                transition=self.transition,
                warnings=self.warnings,
            )
        finally:
            self.client.close()
        for warning in record["warnings"][len(self.warnings) :]:
            out.warn(warning)
        return record

    def close(self) -> None:
        self.client.close()


def _sources(value: Any) -> list:
    if isinstance(value, list | tuple):
        return list(value)
    return [value] if value is not None else []


def _invalid(message: str) -> CliError:
    return CliError(message, ExitCode.INVALID, hint=_CONFIG_HINT)
