"""One policy sync for one tenant (M2-02).

`sync_once` runs inside the tenant's transaction: it takes the tenant's sync lock (a
transaction advisory lock keyed by the tenant, so another sync of the same tenant answers
`busy` and other tenants are never blocked), fetches the tenant's branch into its own work
folder (`policy_workdir/<tenant_id>`, a bare repo), and - for a commit not seen before -
archives it, validates it with the CLI's core code and stores the version. An accepted
version becomes active; a rejected one is stored with its problems and the previous version
stays active. Both are audited in the tenant's chain.

git runs in a worker thread (it blocks); the bundle is `git archive --format=tar`, gzipped
with `mtime=0` and no file name, so one commit always gives the same bytes and digest."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic.definitions.versions import Pins
from agentic.orgapi import API_PREFIX, ActivePolicy, SyncOutcome
from agentic.policy import OrgPolicy, PolicyError, load_policy, tree_digest
from agentic.policy.load import overlay_index, pipeline_digests, version_index
from agentic.policy.portable import portable_path_problems
from agentic_cp.audit import AuditEvent, append
from agentic_cp.gitops import GitError, GitRunner, RepoUrlError, check_repo_url
from agentic_cp.policy_sync.validate import AUDITED, validate_policy_tree
from agentic_cp.routing.generate import routing_plan
from agentic_cp.routing.outbox import gateway_configured, queue_apply
from agentic_cp.settings import Settings
from agentic_cp.tables import policy_active, policy_versions
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import TenantRow, current_tenant

log = logging.getLogger("agentic_cp.policy_sync")

#: The largest policy tree (the uncompressed archive) a tenant may sync.
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
#: The actor of syncs nobody asked for (the worker, a webhook).
SYNC_ACTOR = "system:policy-sync"
#: git modes that are not plain files: a symbolic link and a submodule (R13).
_LINK_MODES = {"120000": "a symbolic link", "160000": "a submodule"}


@dataclass(frozen=True)
class Actor:
    """Who a sync is attributed to in the audit chain."""

    name: str = SYNC_ACTOR
    kind: str = "system"


def bundle_path(version: str) -> str:
    return f"{API_PREFIX}/policy/versions/{version}/bundle"


async def active_policy(conn: AsyncConnection) -> ActivePolicy | None:
    """The transaction's tenant's active policy, or None before its first accepted sync."""
    row = (
        await conn.execute(
            select(
                policy_active.c.version,
                policy_active.c.branch,
                policy_active.c.activated_at,
                policy_versions.c.digest,
                policy_versions.c.tree_digest,
                policy_versions.c.commit_at,
            ).join(
                policy_versions,
                (policy_versions.c.tenant_id == policy_active.c.tenant_id)
                & (policy_versions.c.version == policy_active.c.version),
            )
        )
    ).first()
    if row is None:
        return None
    return ActivePolicy(
        version=row.version,
        branch=row.branch,
        digest=row.digest,
        tree_digest=row.tree_digest,
        activated_at=row.activated_at,
        commit_at=row.commit_at,
        bundle_path=bundle_path(row.version),
    )


async def _active_column(conn: AsyncConnection, column) -> list[str] | None:
    row = (
        await conn.execute(
            select(column).join(
                policy_active,
                (policy_versions.c.tenant_id == policy_active.c.tenant_id)
                & (policy_versions.c.version == policy_active.c.version),
            )
        )
    ).first()
    return list(row[0]) if row is not None else None


async def active_teams(conn: AsyncConnection) -> list[str] | None:
    """The teams of the tenant's active policy, or None when no policy is active (R14)."""
    return await _active_column(conn, policy_versions.c.teams)


async def active_repo_patterns(conn: AsyncConnection) -> list[str] | None:
    """The active policy's `allowed_repo_patterns`, or None when no policy is active."""
    return await _active_column(conn, policy_versions.c.repo_patterns)


async def sync_once(
    engine: AsyncEngine,
    git: GitRunner,
    tenant_id: UUID,
    settings: Settings,
    clock: Callable[[], datetime],
    *,
    actor: Actor | None = None,
) -> SyncOutcome:
    """Fetch, validate and (when valid) activate the tenant's policy branch head."""
    actor = actor or Actor()
    async with tenant_tx(engine, tenant_id) as conn:
        tenant = await current_tenant(conn)
        if not tenant.policy_repo_url:
            return SyncOutcome(kind="unconfigured")
        key = func.hashtextextended(f"agentic.policy_sync:{tenant_id}", 0)
        if not (await conn.execute(select(func.pg_try_advisory_xact_lock(key)))).scalar():
            return SyncOutcome(kind="busy")
        workdir = Path(settings.policy_workdir) / str(tenant_id)
        try:
            commit, commit_at = await asyncio.to_thread(_fetch, git, workdir, tenant)
        except GitError as error:
            log.warning("tenant %s: policy fetch failed: %s", tenant.slug, error)
            return SyncOutcome(kind="failed", problems=[str(error)])
        version = f"git:{commit}"
        stored = (
            await conn.execute(
                select(policy_versions.c.status, policy_versions.c.problems).where(
                    policy_versions.c.version == version
                )
            )
        ).first()
        previous = await active_policy(conn)
        if stored is not None:
            if stored.status == "accepted" and (previous is None or previous.version != version):
                await _activate(conn, tenant, version, previous, clock, actor, settings)
                return SyncOutcome(kind="activated", version=version)
            return SyncOutcome(kind="unchanged", version=version, problems=list(stored.problems))
        try:
            built = await asyncio.to_thread(_build, git, workdir, tenant, commit, version)
        except GitError as error:
            log.warning("tenant %s: policy archive failed: %s", tenant.slug, error)
            return SyncOutcome(kind="failed", version=version, problems=[str(error)])
        await conn.execute(
            policy_versions.insert().values(
                tenant_id=tenant_id,
                version=version,
                commit=commit,
                branch=tenant.policy_branch,
                status="rejected" if built.problems else "accepted",
                problems=built.problems,
                teams=built.teams,
                repo_patterns=built.repo_patterns,
                settings=built.settings,
                bundle=None if built.problems else built.bundle,
                digest=None if built.problems else built.digest,
                tree_digest=None if built.problems else built.tree,
                commit_at=commit_at,
            )
        )
        if built.problems:
            await append(
                conn,
                AuditEvent(
                    event="policy.rejected",
                    actor=actor.name,
                    actor_kind=actor.kind,
                    target=f"policy:{tenant.policy_branch}",
                    reason=built.problems[0],
                    after={"version": version, "problems": built.problems[:AUDITED]},
                    policy_version=version,
                ),
                clock=clock,
            )
            log.info("tenant %s: policy %s rejected", tenant.slug, version)
            return SyncOutcome(kind="rejected", version=version, problems=built.problems)
        await _activate(conn, tenant, version, previous, clock, actor, settings)
        return SyncOutcome(kind="activated", version=version)


async def _activate(
    conn: AsyncConnection,
    tenant: TenantRow,
    version: str,
    previous: ActivePolicy | None,
    clock: Callable[[], datetime],
    actor: Actor,
    settings: Settings,
) -> None:
    now = clock()
    values = {"version": version, "branch": tenant.policy_branch, "activated_at": now}
    await conn.execute(
        insert(policy_active)
        .values(tenant_id=tenant.id, **values)
        .on_conflict_do_update(index_elements=[policy_active.c.tenant_id], set_=values)
    )
    active = await active_policy(conn)
    await append(
        conn,
        AuditEvent(
            event="policy.activated",
            actor=actor.name,
            actor_kind=actor.kind,
            target=f"policy:{tenant.policy_branch}",
            before={"version": previous.version if previous else None},
            after={
                "version": version,
                "branch": tenant.policy_branch,
                "digest": active.digest,
                "tree_digest": active.tree_digest,
            },
            policy_version=version,
        ),
        clock=clock,
    )
    if gateway_configured(settings):  # M2-09: the gateway's routing follows, in this tx
        await queue_apply(conn, version, actor=actor.name, actor_kind=actor.kind, clock=clock)
    log.info("tenant %s: policy %s activated", tenant.slug, version)


# ── git and the bundle (worker thread) ───────────────────────────────────────


@dataclass(frozen=True)
class Built:
    problems: list[str]
    teams: list[str]
    repo_patterns: list[str]
    bundle: bytes
    digest: str
    tree: str
    settings: dict = field(default_factory=dict)  # what run registration reads (M2-08/21)


def _fetch(git: GitRunner, workdir: Path, tenant: TenantRow) -> tuple[str, datetime | None]:
    """Fetch the tenant's branch head into its bare work repo; (commit, commit time)."""
    try:  # again here: a URL stored before the rule, or by hand, is never fetched (R12)
        url = check_repo_url(
            tenant.policy_repo_url or "", allow_local=git.settings.allow_local_repos
        )
    except RepoUrlError as error:
        raise GitError(str(error)) from None
    if not (workdir / "HEAD").is_file():
        workdir.mkdir(parents=True, exist_ok=True)
        git.run(["init", "--bare", "-q"], cwd=workdir, tenant_slug=tenant.slug)
    git.run(
        [
            "fetch",
            "--depth",
            "1",
            "--no-tags",
            "-q",
            url,
            f"refs/heads/{tenant.policy_branch}",
        ],
        cwd=workdir,
        tenant_slug=tenant.slug,
    )
    commit = git.run(
        ["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=workdir, tenant_slug=tenant.slug
    )
    sha = commit.decode("ascii").strip()
    when = git.run(["show", "-s", "--format=%cI", sha], cwd=workdir, tenant_slug=tenant.slug)
    try:
        commit_at = datetime.fromisoformat(when.decode("ascii").strip())
    except ValueError:
        commit_at = None
    return sha, commit_at


def _tree_problems(git: GitRunner, workdir: Path, tenant: TenantRow, commit: str) -> list[str]:
    """What laptops could not extract as it is: links and submodules (R13) and names
    some laptop's file system refuses or folds together (review F4)."""
    listing = git.run(["ls-tree", "-r", "-z", commit], cwd=workdir, tenant_slug=tenant.slug)
    problems, names = [], []
    for item in listing.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode("ascii")
        try:
            name = path.decode("utf-8")
        except UnicodeDecodeError:
            name = path.decode("utf-8", errors="replace")
            problems.append(f"{name}: the name is not UTF-8 (policy.path)")
            continue
        if mode in _LINK_MODES:
            problems.append(
                f"{name}: {_LINK_MODES[mode]} is not allowed in a policy repo "
                "(policy.symlink: laptops extract plain files only)"
            )
        names.append(name)
    return problems + portable_path_problems(names)


def _build(git: GitRunner, workdir: Path, tenant: TenantRow, commit: str, version: str) -> Built:
    """Archive `commit`, then validate the extracted tree and bundle it."""
    problems = _tree_problems(git, workdir, tenant, commit)
    archive = git.run(["archive", "--format=tar", commit], cwd=workdir, tenant_slug=tenant.slug)
    if len(archive) > MAX_BUNDLE_BYTES:
        problems.append(
            f"policy.too_large: the policy tree is {len(archive)} bytes; at most "
            f"{MAX_BUNDLE_BYTES} are served"
        )
    if problems:
        return Built(problems, [], [], b"", "", "")
    with tempfile.TemporaryDirectory(prefix="agentic-cp-policy-") as scratch:
        root = Path(scratch) / "tree"
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            tar.extractall(root, filter="data")
        root.mkdir(exist_ok=True)  # an empty commit archives to no files at all
        found, policy = validate_policy_tree(root, version)
        tree = tree_digest(root)
        settings = {}
        if policy is not None:  # the gateway's routing plan (M2-09) rides with the settings
            try:
                routing = routing_plan(policy).to_settings()
            except Exception as error:  # a rejection with its reason, never a wedged sync
                log.exception("tenant %s: routing for policy %s failed", tenant.slug, version)
                found = [*found, f"routing: the gateway routing cannot be generated ({error})"]
            else:
                settings = {**run_settings(policy, root), "routing": routing}
    teams = sorted(policy.teams) if policy is not None else []
    patterns = list(policy.allowed_repo_patterns) if policy is not None else []
    bundle = gzip_bytes(archive)
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    return Built(found, teams, patterns, bundle, digest, tree, settings)


#: Every key `run_settings` writes; a stored version lacking one is read again from its
#: bundle (`ingest.register.version_settings`).
RUN_SETTINGS_KEYS = (
    "min_cli_version",
    "concurrency",
    "allow_repo_pipelines",
    "pipelines",
    "team_budgets",
    "auto_trip",
    "overlays",
    "versions",
    "learning",
    "run_health",
    "retention",
)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def auto_trip_settings(policy: OrgPolicy) -> dict:
    """`org.yaml: auto_trip` with each policy team's effective thresholds (M2-11), decimals
    as text."""
    trip = policy.auto_trip
    teams = {}
    for team, team_policy in sorted(policy.teams.items()):
        spend, errors = trip.effective(team_policy.auto_trip)
        teams[team] = {"spend_usd_per_hour": _text(spend), "error_rate": _text(errors)}
    return {
        "spend_usd_per_hour": _text(trip.spend_usd_per_hour),
        "error_rate": _text(trip.error_rate),
        "window_minutes": trip.window_minutes,
        "min_steps": trip.min_steps,
        "reset_ratio": _text(trip.reset_ratio),
        "hold_minutes": trip.hold_minutes,
        "auto_clear": trip.auto_clear,
        "teams": teams,
    }


def learning_settings(policy: OrgPolicy) -> dict:
    """`org.yaml: learning` with each policy team's effective thresholds (M3-13)."""
    org = policy.learning
    teams = {}
    for team in sorted(policy.teams):
        found = policy.learning_settings(team)
        teams[team] = {"threshold": found.threshold, "window_days": found.window_days}
    return {
        "threshold": org.threshold,
        "window_days": org.window_days,
        "similarity": format(org.similarity, "g"),
        "teams": teams,
    }


def retention_settings(policy: OrgPolicy) -> dict:
    """`retention.yaml` (M3-06, OR-01): the periods in days (None: kept forever), the legal
    holds and the teams that opted in to trace capture - what the retention job and
    `retention gateway-config` read."""
    return {
        "days": policy.retention_days.model_dump(mode="json"),
        "legal_holds": [hold.model_dump(mode="json") for hold in policy.legal_holds],
        "trace_teams": sorted(team for team, own in policy.teams.items() if own.trace_capture),
    }


def run_settings(policy: OrgPolicy, root: Path) -> dict:
    """What run registration (M2-08, M2-21), budget alerts (M2-16) and auto-trip (M2-11)
    read from an accepted version of the policy repo tree at `root`. Team budgets are
    decimal strings, never floats (DF-26). `pipelines` are the approved pipelines' hashes
    (D33), `overlays` the tree's `overlay_index` (M2-23) and `versions` the rollout,
    deprecations, each team's pins and the tree's `version_index` (M2-15), so registration
    checks them all without the bundle. `run_health` is the zombie window (M3-07) and
    `retention` the periods and legal holds (M3-06)."""
    return {
        "auto_trip": auto_trip_settings(policy),
        "learning": learning_settings(policy),
        "run_health": policy.run_health.model_dump(mode="json"),
        "retention": retention_settings(policy),
        "min_cli_version": policy.min_cli_version,
        "concurrency": dict(policy.concurrency),
        "allow_repo_pipelines": policy.allow_repo_pipelines,
        "pipelines": pipeline_digests(root),
        "team_budgets": {
            team: str(Decimal(str(team_policy.monthly_budget_usd)))
            for team, team_policy in sorted(policy.teams.items())
            if team_policy.monthly_budget_usd is not None
        },
        "overlays": overlay_index(root),
        "versions": {
            "rollout": policy.rollout.model_dump(mode="json"),
            "deprecated": policy.deprecated.model_dump(mode="json"),
            "pins": {
                team: team_policy.pins.model_dump(mode="json")
                for team, team_policy in sorted(policy.teams.items())
                if team_policy.pins != Pins()
            },
            **version_index(root),
        },
    }


def settings_from_bundle(bundle: bytes, version: str) -> dict:
    """`run_settings` read back from a stored bundle, for a version accepted before they
    were stored (migration 0009 adds the column empty, and versions are insert-only, so it
    is never backfilled: review F2). {} when the policy no longer loads - its pipelines are
    then refused, never waved through."""
    return _from_bundle(bundle, version, run_settings)


def routing_from_bundle(bundle: bytes, version: str) -> dict:
    """The routing plan (M2-09) generated again from a stored bundle, for a version stored
    without one or by another generator format; {} when the policy no longer loads."""
    return _from_bundle(bundle, version, lambda policy, root: routing_plan(policy).to_settings())


def _from_bundle(bundle: bytes, version: str, read: Callable[[OrgPolicy, Path], dict]) -> dict:
    with tempfile.TemporaryDirectory(prefix="agentic-cp-policy-") as scratch:
        root = Path(scratch) / "tree"
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(bundle)), mode="r:") as tar:
            tar.extractall(root, filter="data")
        root.mkdir(exist_ok=True)
        try:
            policy = load_policy(root / "policy", version=version)
        except PolicyError:
            return {}
        return read(policy, root)


def gzip_bytes(data: bytes) -> bytes:
    """gzip with no time stamp and no file name: the same input gives the same bytes."""
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(data)
    return out.getvalue()
