"""Per-tenant policy sync (SP15 Task 7, M2-02; Review Focus 3): fetch each tenant's policy
repo over git, validate it with the CLI's own core code, version it as `git:<commit>`, keep
the previous version when a new one is invalid, and bundle it deterministically."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import tarfile
import threading
from datetime import UTC, datetime

import pytest
from policy_repos import break_tiers, commit, git, head, make_policy_repo, url, write
from sqlalchemy import select, update

from agentic.policy import tree_digest
from agentic_cp.audit import read_records
from agentic_cp.gitops import GitRunner
from agentic_cp.policy_sync import service
from agentic_cp.policy_sync.service import active_policy, sync_once
from agentic_cp.policy_sync.worker import run_pass
from agentic_cp.settings import Settings
from agentic_cp.tables import policy_versions, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

_NOW = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)


@pytest.fixture
def settings(db, tmp_path) -> Settings:
    return Settings.from_env(
        {
            "AGENTIC_CP_DATABASE_URL": db.app_dsn,
            "AGENTIC_CP_ALLOW_LOCAL_REPOS": "true",
            "AGENTIC_CP_POLICY_WORKDIR": str(tmp_path / "work"),
        }
    )


async def _point(db, tenant_id, repo_url, branch="main"):
    """Set a tenant's policy source as the owner role (what set-policy-repo does)."""
    async with tenant_tx(db.owner, tenant_id) as conn:
        await conn.execute(
            update(tenants)
            .where(tenants.c.id == tenant_id)
            .values(policy_repo_url=repo_url, policy_branch=branch)
        )


async def _sync(db, settings, tenant_id, git=None, clock=None):
    return await sync_once(
        db.app, git or GitRunner(settings), tenant_id, settings, clock or (lambda: _NOW)
    )


async def _active(db, tenant_id):
    async with tenant_tx(db.app, tenant_id) as conn:
        return await active_policy(conn)


async def test_valid_policy_is_activated_with_git_version(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    await _point(api.db, api.a.id, url(repo))
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "activated", outcome
    assert outcome.version == "git:" + head(repo)
    active = await _active(api.db, api.a.id)
    assert active.version == "git:" + head(repo) and active.branch == "main"
    async with tenant_tx(api.db.app, api.a.id) as conn:
        events = [r.event for r in await read_records(conn)]
    assert events[-1] == "policy.activated"


async def test_invalid_policy_rejected_and_previous_kept(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    await _point(api.db, api.a.id, url(repo))
    good = await _sync(api.db, settings, api.a.id)
    bad_commit = break_tiers(repo)
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "rejected"
    assert outcome.version == "git:" + bad_commit
    assert any("tiers.yaml" in problem for problem in outcome.problems), outcome.problems
    assert (await _active(api.db, api.a.id)).version == good.version
    async with tenant_tx(api.db.app, api.a.id) as conn:
        records = await read_records(conn)
        stored = (
            await conn.execute(
                select(policy_versions.c.status, policy_versions.c.bundle).where(
                    policy_versions.c.version == outcome.version
                )
            )
        ).one()
    assert stored.status == "rejected" and stored.bundle is None
    assert records[-1].event == "policy.rejected" and "tiers.yaml" in records[-1].reason


async def test_each_tenant_syncs_its_own_repo(api, settings, tmp_path):
    repo_a = make_policy_repo(tmp_path / "policy-a")
    repo_b = make_policy_repo(tmp_path / "policy-b", teams=("billing",))
    git(repo_b, "switch", "-q", "-c", "staging")
    write(repo_b, "policy/teams/ops.yaml", "{}\n")
    staging = commit(repo_b, "staging only")
    await _point(api.db, api.a.id, url(repo_a))
    await _point(api.db, api.b.id, url(repo_b), "staging")
    assert (await _sync(api.db, settings, api.a.id)).kind == "activated"
    assert (await _sync(api.db, settings, api.b.id)).kind == "activated"
    assert (await _active(api.db, api.a.id)).version == "git:" + head(repo_a)
    assert (await _active(api.db, api.b.id)).version == "git:" + staging
    work = settings.policy_workdir
    assert (work / str(api.a.id)).is_dir() and (work / str(api.b.id)).is_dir()
    # neither tenant sees the other's versions
    async with tenant_tx(api.db.app, api.a.id) as conn:
        versions = {v for (v,) in await conn.execute(select(policy_versions.c.version))}
    assert versions == {"git:" + head(repo_a)}


async def test_tenant_branch_is_used(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    git(repo, "switch", "-q", "-c", "staging")
    write(repo, "policy/teams/ops.yaml", "{}\n")
    staging = commit(repo, "staging")
    git(repo, "switch", "-q", "main")
    await _point(api.db, api.a.id, url(repo), "staging")
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.version == "git:" + staging


async def test_one_tenant_failing_does_not_stop_others(api, settings, tmp_path, caplog):
    repo_b = make_policy_repo(tmp_path / "policy-b")
    await _point(api.db, api.a.id, (tmp_path / "nowhere").resolve().as_uri())
    await _point(api.db, api.b.id, url(repo_b))
    with caplog.at_level(logging.WARNING, logger="agentic_cp"):
        results = await run_pass(api.db.app, GitRunner(settings), settings, lambda: _NOW)
    assert results["tenant-b"].kind == "activated"
    assert results["tenant-a"].kind == "failed"
    assert any("tenant-a" in record.getMessage() for record in caplog.records)


async def test_unconfigured_tenant_reports_unconfigured(api, settings):
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "unconfigured" and outcome.version is None


async def test_definitions_validated_for_every_team(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    write(repo, "teams/payments/agents/x.md", "---\nname: x\n---\nnot a valid agent\n")
    commit(repo, "broken team agent")
    await _point(api.db, api.a.id, url(repo))
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "rejected"
    assert any("teams/payments/agents/x.md" in problem for problem in outcome.problems)


async def test_unchanged_head_is_not_reprocessed(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    await _point(api.db, api.a.id, url(repo))
    await _sync(api.db, settings, api.a.id)
    again = await _sync(api.db, settings, api.a.id)
    assert again.kind == "unchanged" and again.version == "git:" + head(repo)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        activations = [r for r in await read_records(conn) if r.event == "policy.activated"]
    assert len(activations) == 1


async def test_returning_to_an_accepted_commit_reactivates_it(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    first = head(repo)
    git(repo, "switch", "-q", "-c", "staging")
    write(repo, "policy/teams/ops.yaml", "{}\n")
    commit(repo, "staging")
    await _point(api.db, api.a.id, url(repo))
    await _sync(api.db, settings, api.a.id)
    await _point(api.db, api.a.id, url(repo), "staging")
    await _sync(api.db, settings, api.a.id)
    await _point(api.db, api.a.id, url(repo), "main")
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "activated" and outcome.version == "git:" + first
    assert (await _active(api.db, api.a.id)).version == "git:" + first


async def test_symlink_in_policy_tree_is_rejected(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    target = repo / "target.txt"
    target.write_bytes(b"../../etc/passwd")
    blob = git(repo, "hash-object", "-w", "target.txt")
    target.unlink()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},policy/link.yaml")
    git(repo, "commit", "-q", "-m", "a symlink")
    await _point(api.db, api.a.id, url(repo))
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "rejected"
    assert any("policy/link.yaml" in problem for problem in outcome.problems)


async def test_sync_rejects_a_policy_with_a_refused_overlay(api, settings, tmp_path):
    """M2-23 (Review Focus 5): an overlay removing an org-required gate is caught by the
    sync, which keeps the previous version, never first on a laptop."""
    repo = make_policy_repo(tmp_path / "policy-a")
    write(
        repo,
        "pipelines/deliver.yaml",
        "name: deliver\nversion: 1.0.0\nsteps:\n  - id: build\n    agent: developer\n"
        "  - gate: ship\n    approver_role: approver\n",
    )
    write(  # the sync preflights each team's pipelines (review F2): its agent must exist
        repo,
        "agents/developer.md",
        "---\nname: developer\ntype: general\nversion: 1.0.0\ndescription: d\n"
        "consumes: []\n---\nGo.\n",
    )
    org = (repo / "policy" / "org.yaml").read_text(encoding="utf-8")
    write(repo, "policy/org.yaml", org + "required_steps:\n  deliver: [ship]\n")
    commit(repo, "a pipeline with a required gate")
    await _point(api.db, api.a.id, url(repo))
    good = await _sync(api.db, settings, api.a.id)
    assert good.kind == "activated", good
    write(
        repo,
        "teams/payments/pipelines/overlays/app.yaml",
        "pipelines:\n  deliver:\n    remove: [ship]\n",
    )
    commit(repo, "an overlay dropping the gate")
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "rejected", outcome
    assert any(
        p.startswith("teams/payments/pipelines/overlays/app.yaml: ") and "rule required-step" in p
        for p in outcome.problems
    ), outcome.problems
    assert (await _active(api.db, api.a.id)).version == good.version


@pytest.mark.parametrize(
    "names",
    [
        ("docs/a:b.txt",),
        ("docs/notes.",),
        ("docs/aux.md",),
        ("docs/Readme.md", "docs/README.md"),
        ("Docs/x.md", "docs/y.md"),
    ],
)
async def test_a_name_some_laptop_cannot_extract_is_rejected(api, settings, tmp_path, names):
    """Review F4: Windows and macOS laptops must extract the validated tree as it is; a
    name they refuse, mangle or fold into another is rejected with the file named. The
    entries are added with git plumbing: this file system may not hold them either."""
    repo = make_policy_repo(tmp_path / "policy-a")
    (repo / "blob.txt").write_bytes(b"x\n")
    blob = git(repo, "hash-object", "-w", "blob.txt")
    (repo / "blob.txt").unlink()
    for name in names:  # protectNTFS off: Git for Windows refuses these names by default
        entry = f"100644,{blob},{name}"
        git(repo, "-c", "core.protectNTFS=false", "update-index", "--add", "--cacheinfo", entry)
    git(repo, "commit", "-q", "-m", "an unportable name")
    await _point(api.db, api.a.id, url(repo))
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "rejected", outcome
    assert any(names[-1] in p and "policy.path" in p for p in outcome.problems), outcome


async def test_bundle_too_large_is_rejected(api, settings, tmp_path, monkeypatch):
    repo = make_policy_repo(tmp_path / "policy-a")
    monkeypatch.setattr(service, "MAX_BUNDLE_BYTES", 1024)
    await _point(api.db, api.a.id, url(repo))
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "rejected"
    assert any("policy.too_large" in problem for problem in outcome.problems)


async def test_bundle_digest_matches_and_is_deterministic(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    await _point(api.db, api.a.id, url(repo))
    await _point(api.db, api.b.id, url(repo))
    await _sync(api.db, settings, api.a.id)
    other = settings.model_copy(update={"policy_workdir": tmp_path / "other-work"})
    await _sync(api.db, other, api.b.id)
    rows = []
    for tenant in (api.a.id, api.b.id):
        async with tenant_tx(api.db.app, tenant) as conn:
            rows.append(
                (
                    await conn.execute(
                        select(
                            policy_versions.c.bundle,
                            policy_versions.c.digest,
                            policy_versions.c.tree_digest,
                        )
                    )
                ).one()
            )
    (bundle_a, digest_a, tree_a), (bundle_b, digest_b, tree_b) = rows
    assert digest_a == digest_b and tree_a == tree_b
    assert digest_a == "sha256:" + hashlib.sha256(bundle_a).hexdigest()
    assert bundle_a == bundle_b
    extracted = tmp_path / "extracted"
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(bundle_a))) as archive:
        archive.extractall(extracted, filter="data")
    assert tree_digest(extracted) == tree_a


class RecordingGit(GitRunner):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def _spawn(self, argv, env, cwd, timeout):
        self.calls.append((list(argv), dict(env)))
        return super()._spawn(argv, env, cwd, timeout)


async def test_git_runs_with_autocrlf_off_and_tenant_git_config(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    config_dir = tmp_path / "gitconfig"
    config_dir.mkdir()
    (config_dir / "tenant-a.gitconfig").write_text("[core]\n", encoding="utf-8")
    configured = settings.model_copy(update={"git_config_dir": config_dir})
    await _point(api.db, api.a.id, url(repo))
    recording = RecordingGit(configured)
    await _sync(api.db, configured, api.a.id, git=recording)
    assert recording.calls
    for argv, env in recording.calls:
        assert "core.autocrlf=false" in argv and "protocol.ext.allow=never" in argv
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_GLOBAL"] == str(config_dir / "tenant-a.gitconfig")
    await _point(api.db, api.b.id, url(repo))
    recording_b = RecordingGit(configured)
    await _sync(api.db, configured, api.b.id, git=recording_b)
    assert all("tenant-a" not in env.get("GIT_CONFIG_GLOBAL", "") for _, env in recording_b.calls)


class SlowGit(GitRunner):
    """Holds the fetch until released, so two syncs overlap."""

    def __init__(self, settings, started: asyncio.Event, release, loop):
        super().__init__(settings)
        self.started, self.release, self.loop = started, release, loop

    def _spawn(self, argv, env, cwd, timeout):
        if "fetch" in argv:
            self.loop.call_soon_threadsafe(self.started.set)
            self.release.wait(10)
        return super()._spawn(argv, env, cwd, timeout)


async def test_only_one_sync_at_a_time_per_tenant(api, settings, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    for tenant in (api.a.id, api.b.id):
        await _point(api.db, tenant, url(repo))
    started, release = asyncio.Event(), threading.Event()
    slow = SlowGit(settings, started, release, asyncio.get_running_loop())
    first = asyncio.create_task(_sync(api.db, settings, api.a.id, git=slow))
    await asyncio.wait_for(started.wait(), 10)
    second = await _sync(api.db, settings, api.a.id)
    other = await _sync(api.db, settings, api.b.id)  # not blocked by a's lock
    release.set()
    assert second.kind == "busy"
    assert other.kind == "activated"
    assert (await first).kind == "activated"
