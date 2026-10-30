"""Run registration: the org checks before a run starts (M2-08, M2-21; Review Focus 5) -
minimum CLI version, RBAC, kill switch, policy version, pipeline integrity (D33),
concurrency caps and the team budget (unmeasured: null, never 0)."""

import asyncio
import hashlib
from datetime import timedelta

import pytest
from apiclient import member
from policy_repos import commit, make_policy_repo, url, write
from sqlalchemy import func, select, update

from agentic.orgapi import CLI_VERSION_HEADER
from agentic_cp.tables import policy_versions, runs
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

PIPELINE = (
    "name: deliver\nversion: 1.0.0\nregime: team\n"
    "steps:\n  - gate: review\n    approver_role: developer\n"
)
SHA = "sha256:" + hashlib.sha256(PIPELINE.encode()).hexdigest()
NEW_CLI = {CLI_VERSION_HEADER: "9.9.9"}


def _policy_repo(path, *, org_yaml="allow_repo_pipelines: false\n", per_user=2, per_team=3):
    repo = make_policy_repo(path)
    write(repo, "pipelines/deliver.yaml", PIPELINE)
    write(
        repo,
        "policy/limits.yaml",
        f"concurrency: {{ per_user: {per_user}, per_team: {per_team} }}\n",
    )
    write(repo, "policy/org.yaml", org_yaml)
    commit(repo, "pipelines and limits")
    return repo


async def _sync(api, tenant, repo) -> str:
    made = await api.put(
        "/v1/tenant/policy-source", tenant.admin_token, {"repo": url(repo), "branch": "main"}
    )
    assert made.status_code == 200, made.text
    sync = made.json()["sync"]
    assert sync["kind"] in ("activated", "unchanged"), sync
    return sync["version"]


@pytest.fixture
async def version(api, tmp_path):
    return await _sync(api, api.a, _policy_repo(tmp_path / "policy"))


def _body(version, run_id="run-1", **over):
    body = {
        "run_id": run_id,
        "team": "payments",
        "repo": "github.com/acme/app",
        "pipeline": "deliver",
        "pipeline_file": "pipelines/deliver.yaml",
        "pipeline_sha256": SHA,
        "policy_version": version,
        "agents": ["developer@1.0.0"],
        "models": ["acme/model-b"],
        "backends": ["native"],
    }
    return {**body, **over}


async def _register(api, token, body, headers=NEW_CLI):
    reply = await api.post("/v1/runs", token, body, headers=headers)
    assert reply.status_code == 200, reply.text
    return reply.json()


def _check(result, name):
    return next(c for c in result["checks"] if c["check"] == name)


async def test_old_cli_refused_with_upgrade_message(api, version):
    floor = api.with_settings(min_cli_version="1.2.0")
    old = await floor.post(
        "/v1/runs", api.a.admin_token, _body(version), headers={CLI_VERSION_HEADER: "1.1.9"}
    )
    assert old.status_code == 426, old.text
    problem = old.json()
    assert problem["code"] == "cli.too_old"
    assert "1.1.9" in problem["detail"] and "1.2.0" in problem["detail"]
    assert "upgrade" in problem["hint"]
    missing = await floor.post(
        "/v1/runs", api.a.admin_token, _body(version), headers={CLI_VERSION_HEADER: ""}
    )
    assert missing.status_code == 426
    current = await floor.post(
        "/v1/runs", api.a.admin_token, _body(version), headers={CLI_VERSION_HEADER: "1.2.0"}
    )
    assert current.status_code == 200 and current.json()["min_cli_version"] == "1.2.0"


async def test_policy_minimum_raises_the_deployment_floor(api, tmp_path):
    repo = _policy_repo(tmp_path / "policy", org_yaml="min_cli_version: 2.0.0\n")
    version = await _sync(api, api.a, repo)
    floor = api.with_settings(min_cli_version="1.0.0")
    old = await floor.post(
        "/v1/runs", api.a.admin_token, _body(version), headers={CLI_VERSION_HEADER: "1.5.0"}
    )
    assert old.status_code == 426 and "2.0.0" in old.json()["detail"]


async def test_every_check_passes_and_the_run_holds_a_slot(api, version):
    result = await _register(api, api.a.admin_token, _body(version))
    assert result["registered"] is True
    assert {c["check"] for c in result["checks"]} == {
        "rbac",
        "killswitch",
        "policy",
        "pipeline",
        "versions",
        "concurrency",
        "budget",
    }
    assert all(c["passed"] for c in result["checks"]), result["checks"]
    assert result["team_budget_remaining_usd"] is None  # unmeasured: null, never 0
    assert _check(result, "budget")["value"] is None
    run = await api.get("/v1/runs/run-1", api.a.admin_token)
    assert run.status_code == 200 and run.json()["status"] == "open"
    assert run.json()["head"] == {"seq": 0, "hash": ""}


async def test_rbac_refused_without_run_trigger(api, version):
    auditor, _ = await member(api, api.a, "aud@a.example.test", "auditor")
    result = await _register(api, auditor, _body(version))
    assert result["registered"] is False
    assert not _check(result, "rbac")["passed"]
    assert "run.trigger" in _check(result, "rbac")["detail"]


async def test_kill_switch_refuses_the_run(api, version):
    token = api.a.admin_token
    await api.post(
        "/v1/killswitch",
        token,
        {"scope_kind": "agent", "scope_value": "developer", "reason": "bad agent release"},
    )
    result = await _register(api, token, _body(version))
    assert result["registered"] is False
    check = _check(result, "killswitch")
    assert not check["passed"] and "bad agent release" in check["detail"]
    other = await _register(api, token, _body(version, "run-2", agents=["reviewer@1.0.0"]))
    assert _check(other, "killswitch")["passed"]


async def test_pause_switch_refuses_the_run_too(api, version):
    token = api.a.admin_token
    await api.post(
        "/v1/killswitch",
        token,
        {"scope_kind": "team", "scope_value": "payments", "mode": "pause", "reason": "freeze"},
    )
    result = await _register(api, token, _body(version))
    assert not _check(result, "killswitch")["passed"] and result["registered"] is False


async def test_unknown_policy_version_refused(api, version):
    unknown = "git:" + "d" * 40
    result = await _register(api, api.a.admin_token, _body(version, policy_version=unknown))
    assert not _check(result, "policy")["passed"]
    assert result["registered"] is False


async def test_locally_edited_pipeline_refused_with_the_approved_hash(api, version):
    edited = "sha256:" + "e" * 64
    result = await _register(api, api.a.admin_token, _body(version, pipeline_sha256=edited))
    check = _check(result, "pipeline")
    assert not check["passed"]
    assert SHA in check["detail"] and edited in check["detail"]
    assert result["registered"] is False


async def test_a_version_accepted_before_settings_were_stored_is_checked_in_full(api, version):
    """Review F2: a version accepted before migration 0009 has `settings = {}`; its
    pipelines, caps and minimum CLI version are read from its stored bundle instead of
    refusing every managed pipeline and enforcing no caps."""
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(policy_versions).where(policy_versions.c.version == version).values(settings={})
        )
    token = api.a.admin_token
    first = await _register(api, token, _body(version, "run-1"))
    assert first["registered"], first["checks"]
    assert _check(first, "pipeline")["passed"]
    assert (await _register(api, token, _body(version, "run-2")))["registered"]
    third = await _register(api, token, _body(version, "run-3"))
    assert not _check(third, "concurrency")["passed"]  # per_user 2 from limits.yaml


async def test_unapproved_pipeline_file_refused(api, version):
    result = await _register(
        api, api.a.admin_token, _body(version, pipeline_file="pipelines/other.yaml")
    )
    assert not _check(result, "pipeline")["passed"]


async def test_repo_pipeline_only_when_the_policy_allows_it(api, version, tmp_path):
    repo_pipeline = _body(version, pipeline_file=None, pipeline_sha256="sha256:" + "1" * 64)
    refused = await _register(api, api.a.admin_token, repo_pipeline)
    assert not _check(refused, "pipeline")["passed"]
    allowing = _policy_repo(tmp_path / "allowing", org_yaml="allow_repo_pipelines: true\n")
    allowed_version = await _sync(api, api.a, allowing)
    allowed = await _register(
        api, api.a.admin_token, {**repo_pipeline, "policy_version": allowed_version}
    )
    assert _check(allowed, "pipeline")["passed"] and allowed["registered"]


async def test_per_user_cap_enforced_and_freed_when_a_run_closes(api, version):
    token = api.a.admin_token
    for n in (1, 2):
        assert (await _register(api, token, _body(version, f"run-{n}")))["registered"]
    third = await _register(api, token, _body(version, "run-3"))
    check = _check(third, "concurrency")
    assert not check["passed"] and "per_user" in check["detail"] and "2" in check["detail"]
    assert third["registered"] is False
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(update(runs).where(runs.c.run_id == "run-1").values(status="completed"))
    assert (await _register(api, token, _body(version, "run-3")))["registered"]


async def test_per_team_cap_counts_every_member(api, version):
    dev1, _ = await member(api, api.a, "d1@a.example.test", "developer", "payments")
    dev2, _ = await member(api, api.a, "d2@a.example.test", "developer", "payments")
    assert (await _register(api, dev1, _body(version, "t1")))["registered"]
    assert (await _register(api, dev1, _body(version, "t2")))["registered"]
    assert (await _register(api, dev2, _body(version, "t3")))["registered"]
    fourth = await _register(api, dev2, _body(version, "t4"))
    assert "per_team" in _check(fourth, "concurrency")["detail"]


async def test_a_stale_slot_is_not_counted(api, version):
    token = api.a.admin_token
    for n in (1, 2):
        await _register(api, token, _body(version, f"run-{n}"))
    api.clock.advance(timedelta(hours=25))
    assert (await _register(api, token, _body(version, "run-3")))["registered"]


async def test_two_registrations_at_cap_minus_one_get_one_slot(api, version):
    token = api.a.admin_token
    await _register(api, token, _body(version, "run-1"))
    results = await asyncio.gather(
        _register(api, token, _body(version, "race-a")),
        _register(api, token, _body(version, "race-b")),
    )
    assert sorted(r["registered"] for r in results) == [False, True]


async def test_only_taking_a_slot_waits_for_the_tenant_lock(api, version):
    """M3-22: every check ran under the tenant's registration lock, so at 200 concurrent runs
    registrations queued on it (620 ms each), held pool connections and starved every other
    route. Now a dry run and a refused run never wait on it; only taking a slot does."""
    token = api.a.admin_token
    key = func.hashtextextended(f"agentic.runs.register:{api.a.id}", 0)
    async with tenant_tx(api.db.owner, api.a.id) as holder:
        await holder.execute(select(func.pg_advisory_xact_lock(key)))
        dry = await asyncio.wait_for(_register(api, token, _body(version, dry_run=True)), 10)
        assert dry["registered"] is False and all(c["passed"] for c in dry["checks"])
        refused = _body(version, "run-x", policy_version="git:" + "0" * 40)
        assert not (await asyncio.wait_for(_register(api, token, refused), 10))["registered"]
        taking = asyncio.ensure_future(_register(api, token, _body(version, "run-2")))
        await asyncio.sleep(1.0)
        assert not taking.done()  # the slot decision still waits for the lock (R8)
    assert (await asyncio.wait_for(taking, 10))["registered"]


async def test_caps_of_two_tenants_are_counted_separately(api, version, tmp_path):
    version_b = await _sync(api, api.b, _policy_repo(tmp_path / "policy-b"))
    for n in (1, 2):
        await _register(api, api.b.admin_token, _body(version_b, f"b-{n}"))
    assert (await _register(api, api.a.admin_token, _body(version, "a-1")))["registered"]


async def test_dry_run_registers_nothing(api, version):
    result = await _register(api, api.a.admin_token, _body(version, dry_run=True))
    assert result["registered"] is False and all(c["passed"] for c in result["checks"])
    assert (await api.get("/v1/runs/run-1", api.a.admin_token)).status_code == 404


async def test_a_run_id_registers_once(api, version):
    await _register(api, api.a.admin_token, _body(version))
    again = await api.post("/v1/runs", api.a.admin_token, _body(version), headers=NEW_CLI)
    assert again.status_code == 409 and again.json()["code"] == "runs.exists"


async def test_a_registered_run_then_ingests(api, version, tmp_path):
    from sdlc_commons import ledger as ledger_module
    from sdlc_commons.ledger import RunIdentity, RunLedger

    await _register(api, api.a.admin_token, _body(version, "led-1"))
    ledger = RunLedger(tmp_path, "led-1")
    ledger.open(
        identity=RunIdentity(team="payments", policy_version=version),
        kind="feature",
        project="demo",
        owner="payments",
        criticality="low",
    )
    entries = ledger.events()
    ledger.release()
    ledger_module._IDENTITIES.pop("led-1", None)
    made = await api.post("/v1/runs/led-1/events", api.a.admin_token, {"entries": entries})
    assert made.status_code == 200, made.text
    assert made.json()["accepted"] == len(entries)


async def test_stored_settings_of_an_older_release_are_kept_when_the_bundle_cannot_load(api):
    """Review: a version stored before `team_budgets` keeps its stored settings; the bundle
    only fills in the missing key (here it cannot, being unreadable)."""
    import uuid

    from agentic_cp.ingest.register import version_settings

    stored = {
        "min_cli_version": "1.2.0",
        "concurrency": {},
        "allow_repo_pipelines": False,
        "pipelines": {"pipelines/deliver.yaml": SHA},
    }
    commit_id = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            policy_versions.insert().values(
                tenant_id=api.a.id,
                version=f"git:{commit_id}",
                commit=commit_id,
                branch="main",
                status="accepted",
                bundle=b"not a bundle",
                digest="sha256:" + "0" * 64,
                settings=stored,
            )
        )
    async with tenant_tx(api.db.app, api.a.id) as conn:
        found = await version_settings(conn, f"git:{commit_id}")
    assert {key: found[key] for key in stored} == stored


async def test_a_registered_run_keeps_its_budget_for_alerts(api, version):
    from decimal import Decimal

    from sqlalchemy import select

    await _register(api, api.a.admin_token, _body(version, "b-1", budget_usd="12.50"))
    await _register(api, api.a.admin_token, _body(version, "b-2"))
    async with tenant_tx(api.db.app, api.a.id) as conn:
        found = dict((await conn.execute(select(runs.c.run_id, runs.c.budget_usd))).all())
    assert found == {"b-1": Decimal("12.50"), "b-2": None}


async def test_a_registered_run_keeps_its_pipeline_for_cost(api, version):
    """M3-01: cost by pipeline counts a run that made no gateway call too."""
    from sqlalchemy import select

    await _register(api, api.a.admin_token, _body(version, "p-1"))
    async with tenant_tx(api.db.app, api.a.id) as conn:
        found = dict((await conn.execute(select(runs.c.run_id, runs.c.pipeline))).all())
    assert found == {"p-1": "deliver"}


# ── overlays and versions (M2-23, M2-15; D33 integrity of the resolved pipeline) ─────────

OVERLAY = "pipelines:\n  deliver:\n    max_minutes: 30\n"
OVERLAY_SHA = "sha256:" + hashlib.sha256(OVERLAY.encode()).hexdigest()
PAYMENTS_OVERLAY = "teams/payments/pipelines/overlays/app.yaml"
WEB_OVERLAY = "teams/web/pipelines/overlays/app.yaml"


def _agent(version: str) -> str:
    return (
        f"---\nname: developer\ntype: general\nversion: {version}\ndescription: builds\n"
        "---\nBuild it.\n"
    )


@pytest.fixture
async def overlaid(api, tmp_path):
    """Team payments has an approved overlay for every repository named `app`; team web
    has one too."""
    repo = make_policy_repo(tmp_path / "policy", teams=("payments", "web"))
    write(repo, "pipelines/deliver.yaml", PIPELINE)
    write(repo, PAYMENTS_OVERLAY, OVERLAY)
    write(repo, WEB_OVERLAY, OVERLAY.replace("30", "20"))
    commit(repo, "a pipeline and two overlays")
    return await _sync(api, api.a, repo)


def _overlaid(version, run_id="run-1", **over):
    body = _body(version, run_id, overlay_file=PAYMENTS_OVERLAY, overlay_sha256=OVERLAY_SHA)
    return {**body, **over}


async def test_overlaid_run_passes(api, overlaid):
    result = await _register(api, api.a.admin_token, _overlaid(overlaid))
    check = _check(result, "pipeline")
    assert check["passed"] and result["registered"], result["checks"]
    assert PAYMENTS_OVERLAY in check["detail"]


async def test_edited_overlay_refused(api, overlaid):
    edited = "sha256:" + "e" * 64
    result = await _register(api, api.a.admin_token, _overlaid(overlaid, overlay_sha256=edited))
    check = _check(result, "pipeline")
    assert not check["passed"] and result["registered"] is False
    assert OVERLAY_SHA in check["detail"] and edited in check["detail"]
    assert PAYMENTS_OVERLAY in check["detail"]


async def test_overlay_of_another_team_refused(api, overlaid):
    web_sha = "sha256:" + hashlib.sha256(OVERLAY.replace("30", "20").encode()).hexdigest()
    body = _overlaid(overlaid, overlay_file=WEB_OVERLAY, overlay_sha256=web_sha)
    result = await _register(api, api.a.admin_token, body)
    check = _check(result, "pipeline")
    assert not check["passed"] and result["registered"] is False
    assert "team web" in check["detail"]


async def test_skipped_overlay_refused(api, overlaid):
    """A run that leaves out the overlay its team approved for this repository (an old or
    altered CLI) runs a pipeline nobody approved for it."""
    body = _body(overlaid)  # no overlay_file
    result = await _register(api, api.a.admin_token, body)
    check = _check(result, "pipeline")
    assert not check["passed"] and result["registered"] is False
    assert PAYMENTS_OVERLAY in check["detail"]
    other_repo = _body(overlaid, "run-2", repo="github.com/acme/billing")  # none applies
    assert (await _register(api, api.a.admin_token, other_repo))["registered"]


async def test_overlay_for_another_repository_refused(api, overlaid):
    body = _overlaid(overlaid, repo="github.com/acme/billing")
    check = _check(await _register(api, api.a.admin_token, body), "pipeline")
    assert not check["passed"] and "does not apply" in check["detail"]


@pytest.fixture
async def rolled(api, tmp_path):
    """developer 1.1.0 is rolled out to team web only (1.0.0 archived); a pipeline version
    1.0.0 is archived beside the current deliver."""
    repo = make_policy_repo(tmp_path / "policy", teams=("payments", "web"))
    write(repo, "pipelines/deliver.yaml", PIPELINE.replace("1.0.0", "2.0.0"))
    write(repo, "pipelines/versions/deliver/1.0.0.yaml", PIPELINE)
    write(repo, "agents/developer.md", _agent("1.1.0"))
    write(repo, "agents/versions/developer/1.0.0.md", _agent("1.0.0"))
    blocked = (api.clock.now + timedelta(days=1)).date().isoformat()
    write(
        repo,
        "policy/rollout.yaml",
        "rollout:\n  agents:\n    developer: {version: 1.1.0, previous: 1.0.0, teams: [web]}\n"
        f"deprecated:\n  pipelines:\n    deliver:\n      '1.0.0': {{blocked_from: {blocked}}}\n",
    )
    write(repo, "policy/teams/web.yaml", "pins:\n  pipelines: {deliver: 2.0.0}\n")
    commit(repo, "a rollout, a deprecation and a pin")
    return await _sync(api, api.a, repo)


def _rolled(version, run_id="run-1", **over):
    archived = {
        "pipeline_file": "pipelines/versions/deliver/1.0.0.yaml",
        "pipeline_sha256": SHA,
        "pipeline_version": "1.0.0",
        "agents": ["developer@1.0.0"],
    }
    return {**_body(version, run_id), **archived, **over}


async def test_held_back_team_runs_the_archived_versions(api, rolled):
    result = await _register(api, api.a.admin_token, _rolled(rolled))
    assert result["registered"], result["checks"]
    assert _check(result, "versions")["passed"]
    assert _check(result, "pipeline")["passed"]  # the archived file is approved too


async def test_version_outside_rollout_refused(api, rolled):
    result = await _register(api, api.a.admin_token, _rolled(rolled, agents=["developer@1.1.0"]))
    check = _check(result, "versions")
    assert not check["passed"] and result["registered"] is False
    assert "not rolled out to team payments" in check["detail"]


async def test_pinned_version_mismatch_refused(api, rolled):
    body = _rolled(rolled, team="web", agents=["developer@1.1.0"])
    check = _check(await _register(api, api.a.admin_token, body), "versions")
    assert not check["passed"] and "pins pipeline deliver to 2.0.0" in check["detail"]


async def test_blocked_version_refused_by_server_clock(api, rolled):
    """The deprecation is judged by the control plane's clock, not the laptop's: refused
    from `blocked_from`, warned-only the day before."""
    before = await _register(api, api.a.admin_token, _rolled(rolled, dry_run=True))
    assert _check(before, "versions")["passed"]
    assert "deprecated" in _check(before, "versions")["detail"]  # the warning is shown
    api.clock.advance(timedelta(days=1))
    after = await _register(api, api.a.admin_token, _rolled(rolled))
    check = _check(after, "versions")
    assert not check["passed"] and after["registered"] is False
    assert "deliver@1.0.0 is deprecated and refused since" in check["detail"]


@pytest.fixture
async def pipeline_rolled(api, tmp_path):
    """deliver 2.0.0 is rolled out to team web only (1.0.0 archived); team payments has
    its own developer agent at the version the org rolls out."""
    repo = make_policy_repo(tmp_path / "policy", teams=("payments", "web"))
    write(repo, "pipelines/deliver.yaml", PIPELINE.replace("1.0.0", "2.0.0"))
    write(repo, "pipelines/versions/deliver/1.0.0.yaml", PIPELINE)
    write(repo, "agents/developer.md", _agent("1.1.0"))
    write(repo, "agents/versions/developer/1.0.0.md", _agent("1.0.0"))
    write(repo, "teams/payments/agents/developer.md", _agent("1.1.0"))
    write(
        repo,
        "policy/rollout.yaml",
        "rollout:\n  pipelines:\n    deliver: {version: 2.0.0, previous: 1.0.0, teams: [web]}\n"
        "  agents:\n    developer: {version: 1.1.0, previous: 1.0.0, teams: [web]}\n",
    )
    commit(repo, "a pipeline rollout and a team's own agent")
    return await _sync(api, api.a, repo)


CURRENT_SHA = "sha256:" + hashlib.sha256(PIPELINE.replace("1.0.0", "2.0.0").encode()).hexdigest()


async def test_the_current_pipeline_version_is_the_approved_files(api, pipeline_rolled):
    """The control plane reads the version of an approved current file itself: a run that
    claims the previous version while sending the new file is still outside the rollout."""
    body = _body(
        pipeline_rolled,
        pipeline_sha256=CURRENT_SHA,
        pipeline_version="1.0.0",
        agents=["developer@1.1.0"],
    )
    check = _check(await _register(api, api.a.admin_token, body), "versions")
    assert not check["passed"]
    assert "pipeline deliver@2.0.0 is not rolled out to team payments" in check["detail"]


async def test_a_team_owned_agent_is_not_held_by_the_org_rollout(api, pipeline_rolled):
    """payments' own developer (its layer) is the team's to version: developer@1.1.0 is
    not the org's rolled-out agent for it."""
    body = _body(
        pipeline_rolled,
        pipeline_file="pipelines/versions/deliver/1.0.0.yaml",
        pipeline_version="1.0.0",
        agents=["developer@1.1.0"],
    )
    result = await _register(api, api.a.admin_token, body)
    assert _check(result, "versions")["passed"], result["checks"]
