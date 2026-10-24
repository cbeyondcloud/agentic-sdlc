"""What `agentic doctor` checks (M3-09, spec §20): every prerequisite `agentic init` checks,
then the health of everything a run needs - each check with what it found and, when it is
not fine, how to fix it.

A check's status is one of:

  * `ok`   - fine;
  * `fail` - a run would fail or be refused because of it (doctor exits 1);
  * `warn` - works with a limitation, which the detail and fix explain;
  * `info` - for the person to know (standalone mode, a withheld variable);
  * `skip` - could not be checked here, and the detail says why (no token, no repository).

Doctor changes nothing: it installs, fetches and writes nothing, and its network calls are
three short reads - the gateway's model list (`GET /v1/models`, no model is called), the
control plane's `/me` and, with a hosting token, the policy repo's branch protection
(D33). The checks, in order: the machine (`agentic_cli.readiness`: Python, git, the sandbox
with WSL2 and srt, a container engine, Git Bash on Windows, the gateway key), uv, the
repository and its init, the policy (loads, no placeholder models, an org cache not
expired), the OS keyring, the gateway (reachable, key accepted, every tier model served -
DF-654), the control plane (reachable, signed in), the clock (against the control plane's
or gateway's time), free disk space, this repository's local data against its cap (M3-08),
the environment variables agent commands do not get (names only, DF-366) and the org
policy repo's protection (D33: review by code owners required on its default branch).
"""

from __future__ import annotations

import json
import os
import platform as _platform
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import httpx2
from rich.console import Console

from agentic.auth import TOKENLEAN_KEY, CredentialError, Credentials
from agentic.hosting import HostingError, ProtectionHost, RepoRef, parse_remote
from agentic.hosting.git import remote_url
from agentic.policy import OrgPolicy, SandboxPolicy
from agentic.policy.model import gateway_model
from agentic.sandbox.process import withheld_names
from agentic.transport import probe_gateway
from agentic_cli import __version__, control_plane, hosting, housekeeping, readiness
from agentic_cli.config import user_config
from agentic_cli.errors import CliError
from agentic_cli.standalone import PLACEHOLDER
from agentic_cli.workspace import INIT_STATE, Workspace

Status = Literal["ok", "fail", "warn", "info", "skip"]
#: Clock skew past which tokens and audit times go wrong (fail), and worth a look (warn).
SKEW_FAIL_S = 300
SKEW_WARN_S = 60
#: Free disk space on the repository's volume: below these a run's trees will not fit.
DISK_FAIL_BYTES = 1024**3
DISK_WARN_BYTES = 5 * 1024**3
#: An org policy cache with less than this share of its lifetime left is worth refreshing.
CACHE_WARN_SHARE = 0.1
#: A control-plane token that expires within this is worth renewing now.
TOKEN_WARN = timedelta(days=7)
_MARK = {
    "ok": "[green]ok[/green]",
    "fail": "[red]FAIL[/red]",
    "warn": "[yellow]warn[/yellow]",
    "info": "[blue]info[/blue]",
    "skip": "[dim]skip[/dim]",
}
_INIT_STATUS: dict[str, Status] = {"ok": "ok", "missing": "fail", "warn": "warn", "info": "info"}


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def to_json(self) -> dict:
        return {
            "healthy": self.healthy,
            "version": __version__,
            "checks": [asdict(check) for check in self.checks],
        }

    def show(self, console: Console) -> None:
        head = "[green]healthy[/green]" if self.healthy else "[red]problems found[/red]"
        console.print(f"agentic doctor ({__version__}): {head}", markup=True)
        for check in self.checks:
            console.print(f"  \\[{_MARK[check.status]}] ", end="", markup=True)
            console.print(f"{check.name}: {check.detail}", markup=False)
            if check.fix and check.status not in ("ok", "info"):
                for line in check.fix.splitlines():
                    console.print(f"      {line}", markup=False)


@dataclass
class DoctorDeps:
    """What doctor asks the machine and the network (tests replace them)."""

    which: Callable[[str], str | None] = shutil.which
    run: Callable[[list[str]], tuple[int, str]] = readiness._run
    system: str | None = None
    kernel_release: str | None = None
    credentials: Credentials | None = None
    gateway_transport: httpx2.BaseTransport | None = None
    disk_usage: Callable[[Path], Any] = shutil.disk_usage
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def default_deps() -> DoctorDeps:
    return DoctorDeps()


def run_doctor(cwd: Path, home: Path, env: Mapping[str, str], deps: DoctorDeps) -> DoctorReport:
    """Every check, in order; never raises for a failing check (each is reported)."""
    report = DoctorReport()
    add = report.checks.append
    credentials = deps.credentials or Credentials(environ=env)
    try:
        ws: Workspace | None = Workspace.load(cwd, env=env, home=home)
        settings = ws.settings
    except CliError as error:
        ws, settings = None, _settings(home)
        if "git repository" not in str(error):  # the user config cannot be read
            add(Check("user config", "fail", str(error), error.hint or ""))
    policy, policy_check = _policy(ws)
    sandbox_policy = policy if policy is not None else SimpleNamespace(sandbox=SandboxPolicy())
    for item in readiness.prerequisites(
        policy=sandbox_policy,  # type: ignore[arg-type]
        which=deps.which,
        run=deps.run,
        system=deps.system,
        kernel_release=deps.kernel_release,
        credentials=credentials,
        tree=ws.repo if ws else None,
    ):
        add(Check(item.name, _INIT_STATUS[item.status], item.detail, item.fix))
    add(_uv(deps))
    add(_repository(ws, cwd))
    add(policy_check)
    if ws is not None and policy is not None:
        add(_placeholders(policy))
        add(_policy_cache(ws, home))
    add(_keyring(credentials))
    gateway = _gateway(settings, env, credentials, deps)
    report.checks += gateway.checks
    if gateway.models is not None and policy is not None:
        add(_tier_models(policy, gateway.models, ws.team if ws else None))
    signed_in = _control_plane(settings, env, home, deps.now())
    add(signed_in.check)
    add(_clock(signed_in.date or gateway.date, deps.now()))
    add(_disk(ws.repo if ws else home, deps))
    if ws is not None:
        add(_local_data(ws))
    add(_withheld(env, policy))
    add(_policy_repo_protection(settings, credentials))
    return report


def _settings(home: Path) -> dict:
    try:
        return user_config(home)
    except CliError:
        return {}


# ── the checks ────────────────────────────────────────────────────────────────


def _uv(deps: DoctorDeps) -> Check:
    fix = "install uv (docs.astral.sh/uv): agentic and its tools are installed with it"
    if deps.which("uv") is None:
        return Check("uv", "warn", "not found", fix)
    code, out = deps.run(["uv", "--version"])
    found = out.strip().splitlines()[0] if code == 0 and out.strip() else f"exit {code}"
    return Check("uv", "ok" if code == 0 else "warn", found, fix)


def _repository(ws: Workspace | None, cwd: Path) -> Check:
    if ws is None:
        return Check(
            "repository",
            "skip",
            f"{cwd} is not inside a git repository: its checks are skipped",
            "run doctor inside the repository runs are for",
        )
    state = ws.repo / INIT_STATE
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Check(
            "repository",
            "warn",
            f"{ws.repo}: `agentic init` has not been run here",
            "run `agentic init` in the repository",
        )
    when = str(data.get("initialised_at") or "")[:19]
    ready = "ready" if data.get("ready") else "NOT ready when it ran"
    status: Status = "ok" if data.get("ready") else "warn"
    return Check(
        "repository", status, f"{ws.repo}: initialised {when} ({ready})", "run `agentic init` again"
    )


def _policy(ws: Workspace | None) -> tuple[OrgPolicy | None, Check]:
    if ws is None:
        return None, Check("policy", "skip", "no repository: the policy it uses is not known")
    try:
        policy = ws.policy()
    except CliError as error:
        return None, Check("policy", "fail", str(error), error.hint or "run `agentic init`")
    mode = f"org {ws.settings['org']}" if ws.settings.get("org") else "standalone"
    return policy, Check("policy", "ok", f"{mode}, version {policy.version}")


def _placeholders(policy: OrgPolicy) -> Check:
    names = sorted(name for name, tier in policy.tiers.items() if PLACEHOLDER in tier.model)
    if names:
        return Check(
            "policy: models",
            "fail",
            f"placeholder models in tiers {', '.join(names)}",
            f"replace every '{PLACEHOLDER}' model in policy/tiers.yaml with a model your "
            "gateway serves",
        )
    return Check("policy: models", "ok", f"{len(policy.tiers)} tiers")


def _policy_cache(ws: Workspace, home: Path) -> Check:
    org = ws.settings.get("org")
    if not org:
        return Check("policy: cache", "info", "standalone: no org policy cache")
    cache = home / "policy" / str(org)
    if ws.policy_repo != cache / "repo":
        return Check("policy: cache", "info", f"the policy comes from {ws.policy_repo}")
    try:
        stamp = json.loads((cache / "fetched.json").read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(stamp["fetched_at"])
        expires = datetime.fromisoformat(stamp["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return Check(
            "policy: cache", "fail", f"{cache} has no readable fetched.json", "run `agentic init`"
        )
    now = datetime.now(UTC)
    fix = "run `agentic init` while connected (network or VPN) to refresh it"
    if now > expires:
        return Check(
            "policy: cache",
            "fail",
            f"expired at {expires.isoformat(timespec='minutes')}: offline runs are refused",
            fix,
        )
    left = (expires - now).total_seconds()
    life = max((expires - fetched).total_seconds(), 1.0)
    detail = (
        f"fetched {fetched.isoformat(timespec='minutes')}, "
        f"expires {expires.isoformat(timespec='minutes')}"
    )
    return Check("policy: cache", "warn" if left / life < CACHE_WARN_SHARE else "ok", detail, fix)


def _keyring(credentials: Credentials) -> Check:
    if credentials.in_ci:
        return Check("keyring", "skip", f"CI ({credentials.ci_marker} is set): not used in CI")
    try:
        credentials.find("doctor_probe")
    except CredentialError as error:
        return Check(
            "keyring",
            "fail",
            str(error),
            "fix the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service), "
            "or set the AGENTIC_* variables instead",
        )
    return Check("keyring", "ok", "the OS keyring can be read")


@dataclass
class _Gateway:
    checks: list[Check]
    models: tuple[str, ...] | None = None
    date: datetime | None = None


def _gateway(
    settings: Mapping[str, Any], env: Mapping[str, str], credentials: Credentials, deps: DoctorDeps
) -> _Gateway:
    url = env.get("AGENTIC_GATEWAY_URL", "").strip() or str(settings.get("gateway_url") or "")
    if not url:
        fix = "set gateway_url in ~/.agentic/config.yaml (or AGENTIC_GATEWAY_URL)"
        return _Gateway([Check("gateway", "fail", "no model gateway is configured", fix)])
    try:
        found = credentials.find(TOKENLEAN_KEY)
    except CredentialError:
        found = None
    if found is None:
        return _Gateway(
            [Check("gateway", "skip", f"{url}: no key to check it with (see credential)")]
        )
    probe = probe_gateway(url, found.value, transport=deps.gateway_transport)
    if probe.verdict == "accepted":
        check = Check("gateway", "ok", f"{url}: reachable, key accepted")
    elif probe.verdict == "rejected":
        check = Check(
            "gateway",
            "fail",
            f"{url}: {probe.detail}",
            "run `agentic login` with a current key (ask your org admin for a new one)",
        )
    else:
        check = Check(
            "gateway",
            "fail",
            f"{url}: {probe.detail}",
            "check the network, VPN and proxy settings (HTTPS_PROXY), and gateway_url",
        )
    return _Gateway([check], probe.models, probe.server_date)


def _tier_models(policy: OrgPolicy, served: tuple[str, ...], team: str | None) -> Check:
    """DF-654: every tier's model (and the team's own) is one the gateway serves, by the
    name the client sends (`gateway_model`) - else the run fails at its first call."""
    tiers = dict(policy.tiers)
    tiers.update(policy.team_overrides.get(team or "", {}))
    known = set(served)
    missing = sorted(
        f"{name} ({spec.model})"
        for name, spec in tiers.items()
        if gateway_model(spec.model) not in known and spec.model not in known
    )
    if missing:
        return Check(
            "gateway: models",
            "fail",
            f"the gateway does not serve the model of tier(s) {', '.join(missing)}",
            "ask your org admin to correct tiers.yaml, or the gateway's model routing",
        )
    return Check("gateway: models", "ok", f"every tier's model is served ({len(tiers)} tiers)")


@dataclass
class _SignedIn:
    check: Check
    date: datetime | None = None


def _control_plane(
    settings: Mapping[str, Any], env: Mapping[str, str], home: Path, now: datetime
) -> _SignedIn:
    if not settings.get("org"):
        return _SignedIn(Check("control plane", "info", "standalone: no control plane"))
    try:
        client = control_plane.client_for(env, settings, home)
    except control_plane.ControlPlaneError as error:
        return _SignedIn(Check("control plane", "fail", str(error), error.hint or ""))
    if client is None:
        return _SignedIn(
            Check("control plane", "info", "org mode without a control plane (policy repo only)")
        )
    with client:
        try:
            me, date = client.probe_me()
        except control_plane.ControlPlaneError as error:
            return _SignedIn(Check("control plane", "fail", str(error), error.hint or ""))
    expires = me.token_expires_at
    detail = (
        f"signed in as {me.user.email} (tenant {me.tenant.slug}); the token expires "
        f"{expires.date().isoformat()}"
    )
    if expires - now < TOKEN_WARN:
        fix = f"run `{control_plane.LOGIN}` before it expires"
        return _SignedIn(Check("control plane", "warn", detail, fix), date)
    return _SignedIn(Check("control plane", "ok", detail), date)


def _clock(server: datetime | None, now: datetime) -> Check:
    if server is None:
        return Check("clock", "skip", "no server time to compare with")
    skew = (now - server).total_seconds()
    way = "ahead of" if skew > 0 else "behind"
    detail = f"{abs(skew):.0f} s {way} the server"
    fix = "turn on automatic time sync (Windows: Settings > Time; macOS: Date & Time)"
    if abs(skew) > SKEW_FAIL_S:
        return Check("clock", "fail", detail + ": sign-in tokens and audit times go wrong", fix)
    if abs(skew) > SKEW_WARN_S:
        return Check("clock", "warn", detail, fix)
    return Check("clock", "ok", detail)


def _disk(where: Path, deps: DoctorDeps) -> Check:
    try:
        free = deps.disk_usage(where).free
    except OSError as error:
        return Check("disk space", "skip", f"cannot read the free space of {where} ({error})")
    detail = f"{housekeeping.human_bytes(free)} free on the volume of {where}"
    fix = "free some space: `agentic gc` removes finished runs and their trees"
    if free < DISK_FAIL_BYTES:
        return Check("disk space", "fail", detail, fix)
    if free < DISK_WARN_BYTES:
        return Check("disk space", "warn", detail, fix)
    return Check("disk space", "ok", detail)


def _local_data(ws: Workspace) -> Check:
    try:
        warning = housekeeping.cap_warning(ws)
    except CliError as error:
        return Check("local data", "warn", str(error), error.hint or "")
    if warning:
        return Check("local data", "warn", warning, "run `agentic gc --dry-run`, then `agentic gc`")
    return Check("local data", "ok", "under its cap")


def _withheld(env: Mapping[str, str], policy: OrgPolicy | None) -> Check:
    """DF-366: the variables agent commands do not get - by name, never a value."""
    passed = {name.upper() for name in (policy.sandbox.pass_env if policy else [])}
    names = [n for n in withheld_names(env) if n.upper() not in passed]
    if not names:
        return Check("withheld variables", "ok", "agent commands get the whole environment")
    return Check(
        "withheld variables",
        "info",
        "agent commands do not get (on purpose: a secret name or a credential in the "
        f"value): {', '.join(names)}",
        "if a tool needs one (an authenticated proxy, the SSH agent), ask your org admin to "
        "list it in policy sandbox.pass_env",
    )


def _policy_repo_protection(settings: Mapping[str, Any], credentials: Credentials) -> Check:
    """D33: the org policy repo's default branch requires review by code owners, so only
    org admins and team leads change pipelines and policy."""
    name = "policy repo protection"
    if not settings.get("org"):
        return Check(name, "skip", "standalone: no org policy repo")
    repo = _policy_repo_ref(str(settings.get("policy_repo") or ""))
    if repo is None:
        return Check(
            name,
            "skip",
            "cannot check: no hosted policy repo in the user config (policy_repo)",
            "set policy_repo to the policy repo's URL to have it checked",
        )
    try:
        connection = hosting.connect_to(repo, settings, credentials=credentials)
    except CliError as error:
        return Check(name, "skip", f"cannot check: {error}", error.hint or "")
    with connection:
        provider = connection.provider
        if not isinstance(provider, ProtectionHost):
            return Check(name, "skip", f"cannot check: {connection.kind} hosts are not read yet")
        try:
            branch = provider.default_branch(repo)
            found = provider.branch_protection(repo, branch)
        except HostingError as error:
            return Check(name, "skip", f"cannot check: {error}", connection.token_hint)
    where = f"{repo.host}/{repo.slug} {branch}"
    if found.protected and found.code_owner_reviews:
        approvals = f"{found.required_approvals} approval(s)"
        detail = f"{where}: code-owner review required ({found.source}, {approvals})"
        return Check(name, "ok", detail)
    if not found.protected and not found.classic_readable:
        return Check(
            name,
            "skip",
            f"cannot check {where}: the token cannot read its branch protection and no "
            "ruleset requires review",
            "use a token with admin read access to the policy repo, or ask an org admin to "
            "run doctor",
        )
    return Check(
        name,
        "fail",
        f"{where}: review by code owners is not required - anyone with write access could "
        "change org pipelines and policy (D33)",
        "protect the branch: require a pull request with review from Code Owners (branch "
        "protection, or a ruleset with 'require review from code owners')",
    )


def _policy_repo_ref(source: str) -> RepoRef | None:
    """The hosted repository of the policy repo: its URL, or a local clone's origin."""
    if not source:
        return None
    try:
        return parse_remote(source)
    except HostingError:
        pass
    folder = Path(source).expanduser()
    if not folder.is_dir():
        return None
    try:
        return parse_remote(remote_url(folder, "origin"))
    except HostingError:
        return None


def versions(run: Callable[[list[str]], tuple[int, str]] = readiness._run) -> dict[str, str]:
    """What the support bundle and the platform team want to know first."""
    code, git = run(["git", "--version"])
    return {
        "agentic": __version__,
        "python": sys.version.split()[0],
        "os": f"{_platform.system()} {_platform.release()} ({_platform.machine()})",
        "git": git.strip() if code == 0 else "not found",
        "path_separator": os.sep,
    }
