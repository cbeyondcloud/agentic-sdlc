"""Run registration: the org checks before a run starts and its concurrency slot (M2-08,
M2-21; spec §8.2, §12.7, §12.8, D33).

`require_cli_version` refuses an old CLI first (426 `cli.too_old`, ruling R7): the minimum
is the higher of the deployment's `min_cli_version` and the active policy's. Then every
check runs and is reported - `rbac` (`run.trigger`), `killswitch` (a kill or pause switch
matching the run or one of its steps), `policy` (the run's policy version is one this
tenant accepted, ND-7), `pipeline` (the pipeline's bytes hash to the approved file of that
version, and its overlay the approved overlay of the run's team for that repository and
pipeline, never left out - M2-23; a repo-layer pipeline only where the policy allows repo
pipelines), `versions` (the agents' and pipeline's versions against the rollout, the
team's pins and deprecations, by the server's clock - M2-15), `concurrency`
(`per_user` and `per_team` of that version's `limits.yaml`) and `budget` (passed, value
None: the gateway does not measure a team's remaining budget yet - never reported as 0).
The run is registered only when every check passes and it is not a dry run; registrations
of a tenant serialise on an advisory lock, so two at cap-1 get one slot (ruling R8: a slot
is an open run active within `run_slot_hours`)."""

from __future__ import annotations

import asyncio
import tarfile
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from packaging.version import InvalidVersion, Version
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.definitions.overlay import approved_overlays
from agentic.definitions.versions import (
    Deprecations,
    Kind,
    Pins,
    Rollout,
    archived_version,
    version_problem,
)
from agentic.killswitch import RUN, RunScope, Verdict, decide
from agentic.orgapi import OrgCheck, RunPreflight, RunRegistration
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.errors import ApiProblem
from agentic_cp.killswitch.store import active_switches
from agentic_cp.policy_sync.service import (
    RUN_SETTINGS_KEYS,
    active_policy,
    settings_from_bundle,
)
from agentic_cp.rbac.authz import EnforcerCache, Repo, Team, authorize
from agentic_cp.settings import Settings
from agentic_cp.tables import policy_versions, runs
from agentic_cp.tenancy.context import require_tenant_id

#: Settings read back from the bundles of versions accepted before they were stored, by
#: (tenant, version): an accepted version never changes (insert-only), so neither do they.
_DERIVED: dict[tuple[UUID, str], dict] = {}
_DERIVED_MAX = 256

UPGRADE_HINT = (
    "upgrade the agentic CLI to {minimum} or later (`uv tool upgrade agentic-cli`, or the "
    "installer your org uses), then run the command again"
)


def _parsed(text: str | None) -> Version | None:
    try:
        return Version(text) if text else None
    except InvalidVersion:
        return None


def minimum_version(*candidates: str | None) -> str | None:
    """The highest valid version among `candidates` (an invalid one is ignored)."""
    found = [(v, text) for text in candidates if (v := _parsed(text)) is not None]
    return max(found)[1] if found else None


def cli_version_problem(sent: str | None, minimum: str | None) -> str | None:
    """Why a CLI that sent `sent` may not start runs, or None."""
    if minimum is None:
        return None
    have = _parsed((sent or "").strip())
    if have is None:
        return f"this CLI did not send a version it can be checked by; the minimum is {minimum}"
    if have < Version(minimum):
        return f"agentic-cli {sent} is older than the minimum version {minimum}"
    return None


async def version_settings(conn: AsyncConnection, version: str) -> dict | None:
    """The settings of an accepted version, or None when the tenant never accepted it. A
    version accepted before settings were stored has `{}`, and one stored by an older
    release lacks a key this release reads (`team_budgets`, M2-16): they are read from its
    bundle once per process (review F2)."""
    accepted = (policy_versions.c.version == version) & (policy_versions.c.status == "accepted")
    row = (await conn.execute(select(policy_versions.c.settings).where(accepted))).first()
    if row is None:
        return None
    stored = dict(row.settings or {})
    if stored and set(RUN_SETTINGS_KEYS) <= set(stored):
        return stored
    key = (await require_tenant_id(conn), version)
    if key not in _DERIVED:
        bundle = (await conn.execute(select(policy_versions.c.bundle).where(accepted))).scalar()
        try:
            derived = await asyncio.to_thread(settings_from_bundle, bundle, version)
        except (OSError, EOFError, tarfile.TarError):  # an unreadable bundle derives nothing
            derived = {}
        if len(_DERIVED) >= _DERIVED_MAX:
            _DERIVED.pop(next(iter(_DERIVED)))
        _DERIVED[key] = derived
    return {**_DERIVED[key], **stored}  # what was stored wins; the bundle fills the gaps


async def require_cli_version(
    conn: AsyncConnection, sent: str | None, settings: Settings
) -> str | None:
    """The minimum CLI version in force (None: none); 426 `cli.too_old` when `sent` is older."""
    active = await active_policy(conn)
    org = (await version_settings(conn, active.version) or {}) if active is not None else {}
    minimum = minimum_version(settings.min_cli_version, org.get("min_cli_version"))
    if problem := cli_version_problem(sent, minimum):
        raise ApiProblem(426, "cli.too_old", problem, hint=UPGRADE_HINT.format(minimum=minimum))
    return minimum


def _strongest(verdicts: Iterable[Verdict]) -> Verdict:
    found = list(verdicts)
    for action in ("kill", "pause"):
        for verdict in found:
            if verdict.action == action:
                return verdict
    return RUN


async def _killswitch(conn: AsyncConnection, ctx: TokenContext, body: RunRegistration) -> OrgCheck:
    switches = await active_switches(conn)
    base = RunScope(team=body.team, user=ctx.email.lower(), repo=body.repo, pipeline=body.pipeline)
    scopes = [base]
    scopes += [base.with_step(agent=a, model=None, backend=None) for a in body.agents]
    scopes += [base.with_step(agent=None, model=m, backend=None) for m in body.models]
    scopes += [base.with_step(agent=None, model=None, backend=b) for b in body.backends]
    verdict = _strongest(decide(switches, scope) for scope in scopes)
    if verdict.action == "run":
        return OrgCheck(check="killswitch", passed=True, detail="no switch applies")
    return OrgCheck(
        check="killswitch",
        passed=False,
        detail=verdict.describe(),
        value={"mode": verdict.action, "switch": verdict.switch},
    )


async def _rbac(conn, ctx: TokenContext, body: RunRegistration, cache: EnforcerCache) -> OrgCheck:
    resource = Repo(body.repo, team=body.team) if body.repo else Team(body.team)
    decision = await authorize(ctx, "run.trigger", resource, conn=conn, cache=cache)
    where = f"repo {body.repo}" if body.repo else f"team {body.team}"
    if decision.allowed:
        return OrgCheck(
            check="rbac", passed=True, detail=f"run.trigger held as {decision.role} for {where}"
        )
    return OrgCheck(check="rbac", passed=False, detail=f"you do not hold run.trigger for {where}")


def _pipeline(body: RunRegistration, settings: dict | None) -> OrgCheck:
    if settings is None:
        return OrgCheck(
            check="pipeline",
            passed=False,
            detail="cannot be verified: the run's policy version is not one this tenant accepted",
        )
    approved: dict = settings.get("pipelines") or {}
    if body.pipeline_file is None:
        if settings.get("allow_repo_pipelines"):
            return OrgCheck(
                check="pipeline",
                passed=True,
                detail="a repo pipeline, allowed by org policy (allow_repo_pipelines)",
                value=body.pipeline_sha256,
            )
        return OrgCheck(
            check="pipeline",
            passed=False,
            detail="a repo pipeline: org policy runs only approved pipelines from the policy "
            "repo (allow_repo_pipelines is false)",
        )
    expected = approved.get(body.pipeline_file)
    if expected is None:
        return OrgCheck(
            check="pipeline",
            passed=False,
            detail=f"{body.pipeline_file} is not an approved pipeline of this policy version",
        )
    if expected != body.pipeline_sha256:
        return OrgCheck(
            check="pipeline",
            passed=False,
            detail=f"{body.pipeline_file} does not match the approved version: approved "
            f"{expected}, this copy {body.pipeline_sha256} (it was edited locally; run "
            "`agentic init` to restore it)",
            value=expected,
        )
    if problem := _overlay_problem(body, settings.get("overlays") or {}):
        return OrgCheck(check="pipeline", passed=False, detail=problem, value=expected)
    detail = "matches the approved version"
    if body.overlay_file is not None:
        detail += f", with the approved overlay {body.overlay_file}"
    return OrgCheck(check="pipeline", passed=True, detail=detail, value=expected)


def _overlay_problem(body: RunRegistration, index: dict) -> str | None:
    """Why the run's overlay is not the approved one for its team, repository and
    pipeline (M2-23, D33), or None. The approved hash covers the base and the overlay, and
    a run may not leave out the overlay its team approved for this repository."""
    applies = approved_overlays(index, team=body.team, repo=body.repo, pipeline=body.pipeline)
    if body.overlay_file is None:
        if applies:
            return (
                f"the run skips the approved overlay {applies[0]} of team {body.team} for "
                f"{body.repo} (an old or altered CLI? upgrade it and run `agentic init`)"
            )
        return None
    entry = index.get(body.overlay_file)
    if entry is None:
        return f"{body.overlay_file} is not an approved overlay of this policy version"
    if entry.get("team") != body.team:
        return (
            f"{body.overlay_file} is an overlay of team {entry.get('team')}, not of this "
            f"run's team {body.team}"
        )
    if body.overlay_file not in applies:
        return (
            f"{body.overlay_file} does not apply to pipeline {body.pipeline} in "
            f"{body.repo or 'a repository without an origin'}"
        )
    if entry.get("sha256") != body.overlay_sha256:
        return (
            f"{body.overlay_file} does not match the approved version: approved "
            f"{entry.get('sha256')}, this copy {body.overlay_sha256} (it was edited locally; "
            "run `agentic init` to restore it)"
        )
    return None


def _versions(body: RunRegistration, settings: dict | None, today: date) -> OrgCheck:
    """The versions the run uses against the rollout, the team's pins and deprecations,
    by the control plane's clock (M2-15): a blocked version, a version other than the
    team's pin and a new version of a team outside its rollout are refused."""
    if settings is None:
        return OrgCheck(
            check="versions",
            passed=False,
            detail="cannot be verified: the run's policy version is not one this tenant accepted",
        )
    rules = settings.get("versions") or {}
    rollout = Rollout.model_validate(rules.get("rollout") or {})
    deprecated = Deprecations.model_validate(rules.get("deprecated") or {})
    pins = Pins.model_validate((rules.get("pins") or {}).get(body.team) or {})
    # the rollout names org definitions: one the team's own layer defines is not held back
    owned = (rules.get("team_owned") or {}).get(body.team) or {}
    used: list[tuple[Kind, str, str]] = []
    for agent in body.agents:
        name, _, version = agent.rpartition("@")
        if name and version:
            used.append(("agent", name, version))
    if body.pipeline_file is not None:  # a repo pipeline is never versioned
        # the version of the approved file itself (its hash was checked), never only the
        # one the CLI states: an archived file's path names it, a current file declares it
        version = archived_version(body.pipeline_file, "pipeline", body.pipeline) or (
            (rules.get("files") or {}).get(body.pipeline_file) or body.pipeline_version
        )
        if version:
            used.append(("pipeline", body.pipeline, version))
    problems, warnings = [], []
    for kind, name, version in used:
        if problem := version_problem(
            kind,
            name,
            version,
            rollout=Rollout() if name in owned.get(f"{kind}s", ()) else rollout,
            pins=pins,
            deprecated=deprecated,
            team=body.team,
            today=today,
        ):
            problems.append(problem)
        elif (gone := deprecated.of(kind, name, version)) is not None:
            warnings.append(
                f"{kind} {name}@{version} is deprecated, refused from "
                f"{gone.blocked_from.isoformat()}"
            )
    if problems:
        return OrgCheck(check="versions", passed=False, detail="; ".join(problems))
    detail = "; ".join(warnings) if warnings else "every version is one the team may run"
    return OrgCheck(check="versions", passed=True, detail=detail)


async def _concurrency(
    conn: AsyncConnection,
    ctx: TokenContext,
    body: RunRegistration,
    settings: dict | None,
    since: datetime,
) -> OrgCheck:
    caps = (settings or {}).get("concurrency") or {}
    active = (runs.c.status == "open") & (
        func.coalesce(runs.c.last_event_at, runs.c.opened_at) > since
    )
    mine = (
        await conn.execute(
            select(func.count()).select_from(runs).where(active, runs.c.started_by == ctx.user_id)
        )
    ).scalar_one()
    team = (
        await conn.execute(
            select(func.count()).select_from(runs).where(active, runs.c.team == body.team)
        )
    ).scalar_one()
    value = {
        "user": mine,
        "team": team,
        "per_user": caps.get("per_user"),
        "per_team": caps.get("per_team"),
    }
    for name, count in (("per_user", mine), ("per_team", team)):
        cap = caps.get(name)
        if isinstance(cap, int) and count >= cap:
            who = "you have" if name == "per_user" else f"team {body.team} has"
            return OrgCheck(
                check="concurrency",
                passed=False,
                detail=f"{who} {count} runs going; the cap is {name} {cap} (wait for one to "
                "finish, or stop one)",
                value=value,
            )
    return OrgCheck(check="concurrency", passed=True, detail="within the caps", value=value)


async def register_run(
    conn: AsyncConnection,
    ctx: TokenContext,
    body: RunRegistration,
    *,
    cache: EnforcerCache,
    settings: Settings,
    now: datetime,
    minimum: str | None,
) -> RunPreflight:
    """Every org check for `body` in the caller's tenant; registers the run when all pass."""
    tenant_id = await require_tenant_id(conn)
    key = func.hashtextextended(f"agentic.runs.register:{tenant_id}", 0)
    await conn.execute(select(func.pg_advisory_xact_lock(key)))
    exists = await conn.execute(select(runs.c.run_id).where(runs.c.run_id == body.run_id))
    if exists.first() is not None:
        raise ApiProblem(409, "runs.exists", f"run {body.run_id} is already registered")
    version = await version_settings(conn, body.policy_version)
    policy = (
        OrgCheck(check="policy", passed=True, detail=f"{body.policy_version} is accepted")
        if version is not None
        else OrgCheck(
            check="policy",
            passed=False,
            detail=f"{body.policy_version} is not a policy version this tenant accepted (a "
            "locally edited policy?)",
        )
    )
    checks = [
        await _rbac(conn, ctx, body, cache),
        await _killswitch(conn, ctx, body),
        policy,
        _pipeline(body, version),
        _versions(body, version, now.astimezone(UTC).date()),
        await _concurrency(
            conn, ctx, body, version, now - timedelta(hours=settings.run_slot_hours)
        ),
        OrgCheck(check="budget", passed=True, detail="not measured yet", value=None),
    ]
    registered = all(check.passed for check in checks) and not body.dry_run
    if registered:
        await conn.execute(
            runs.insert().values(
                tenant_id=tenant_id,
                run_id=body.run_id,
                team=body.team,
                started_by=ctx.user_id,
                policy_version=body.policy_version,
                policy_known=True,
                opened_at=now,
                budget_usd=Decimal(body.budget_usd) if body.budget_usd else None,  # M2-16
                pipeline=body.pipeline,  # M3-01: cost by pipeline
            )
        )
    return RunPreflight(registered=registered, checks=checks, min_cli_version=minimum)
