"""Acceptance 3 (spec §1.3, M2-53): an org admin changes a tier -> model mapping and a tool
restriction in the tenant's policy repo; the next run honours both, and the run record
shows the policy version it used.

What runs for real: the merged control plane (`_org_stack`: its operator commands, the API
over HTTP, forced row-level security, the audit chain) syncing a real git policy repo; the
admin's API calls; the worker's routing apply writing the gateway's overlay; the `agentic`
CLI in process in org mode - `login --for control-plane`, `init --repo` (its pull request
through the GitHub adapter, the repository registered), `init` (the bundle from the control
plane), `run` and `run --dry-run` with the org checks, registration, kill-switch polls and
event ingest against that control plane; the native backend over HTTP; real git and the
sample repository's tests.

What is stubbed: the models (a recorded cassette of a scripted upstream, standing where
TokenLean stands), TokenLean's routing admin API (`FakeRouting`), GitHub's API
(`FakeGitHub`), the person merging init's pull request, `agentic init`'s code index build,
and the OS sandbox on Windows native.

The admin's three versions:
  v1  the template with real models: `balanced` is acme/balanced-model.
  v2  `balanced` -> acme/balanced-model-v2, and `search` forbidden while the agent types
      still grant it: the next run is refused at preflight, before any model call.
  v3  `search` taken out of the agent types too: invisible to the agents. The next run
      completes, and the gateway is asked for the v2 model with no `search` function.
"""

import json

from _acceptance_support import HOST, MODELS, Scenario, git, sandbox_available, tool
from _invoice_story import PYTEST, REQUEST, design, passing, requirements
from _org_stack import (
    PIPELINE,
    TEAM,
    commit,
    edit,
    org_mode,
    org_policy_repo,
    sign_in,
)

from agentic.policy import gateway_model
from agentic_cli.exit_codes import ExitCode

SCENARIO = "m2-53"
DEVELOPER = "dev@example.invalid"  # the sample repository's git identity, too
BALANCED_V1 = MODELS["change-me/balanced-model"]
BALANCED_V2 = "acme/balanced-model-v2"
DEEP = MODELS["change-me/deep-reasoning-model"]


def one_run() -> dict[str, list]:
    return {
        "requirements": [requirements()],
        "baseline": [tool("run_shell", command=PYTEST), passing("Baseline: 2 pass.", 2, ())],
        "design": [design()],
    }


def script() -> dict[str, list]:
    """Two runs complete (v1 and v3); the run refused at v2 never asks for a model."""
    first, last = one_run(), one_run()
    return {step: first[step] + last[step] for step in first}


def run_org_plan(scenario: Scenario, *extra: str, code: int = ExitCode.OK):
    return scenario.ok(
        "--json", "run", PIPELINE, "--input", f"request={REQUEST}", *extra, code=code
    )


def calls_of(scenario: Scenario, version: str) -> list[dict]:
    """What the gateway was asked by the runs on policy `version` (its identity header)."""
    return [call for call in scenario.server.seen if call["policy_version"] == version]


def test_admin_override_on_the_org_stack(scenario: Scenario, org, tmp_path):
    repo = scenario.existing_repo()
    policy = org_policy_repo(
        tmp_path / "policy-repo", MODELS, sandbox=sandbox_available(), hosts=f"{HOST}/acme/*"
    )

    # ── v1: the admin connects the policy repo; the gateway's routing follows ──────────
    synced = org.connect_policy(policy)
    assert synced["kind"] == "activated", synced
    v1 = synced["version"]
    assert org.apply_routing().status == "applied"
    assert org.tier_models()["balanced"] == gateway_model(BALANCED_V1)

    # ── the developer: a role from the admin, signed in, the repo and the laptop set up ──
    token = org.person(DEVELOPER, "developer", team=TEAM)
    org_mode(scenario, org)
    assert sign_in(scenario, token)["tenant"] == "acme"
    setup = scenario.ok("--json", "init", "--repo", "--team", TEAM).json()
    assert setup["pull_request"]["opened"] is True, setup
    scenario.merge_setup_pull_request(repo, setup["branch"])
    scenario.ok("--json", "init")
    state = json.loads((repo / ".agentic" / "local" / "init.json").read_text(encoding="utf-8"))
    assert (state["mode"], state["policy_version"]) == ("org", v1)  # the bundle it was served
    main_head = git(repo, "rev-parse", "HEAD")
    scenario.models(script())

    first = run_org_plan(scenario).json()
    assert first["status"] == "completed", first
    assert first["policy_version"] == v1
    asked = calls_of(scenario, v1)
    assert asked, scenario.server.seen
    for call in (c for c in asked if c["step"] in ("requirements", "baseline")):
        assert (call["tier"], call["model"]) == ("balanced", gateway_model(BALANCED_V1))
        assert "search" in call["tools"]
    # DF-653 (the CLI half): the tier header carries exactly what the generated rule matches
    assert {call["tier"] for call in asked} <= set(org.tier_models())
    held = org.ok("GET", f"/v1/runs/{first['run_id']}", token)
    assert (held["policy_version"], held["policy_known"]) == (v1, True)

    # ── v2: a new model for `balanced`, and `search` forbidden ──────────────────────────
    edit(policy / "policy" / "tiers.yaml", BALANCED_V1, BALANCED_V2)
    edit(policy / "policy" / "tools.yaml", "forbidden: []", "forbidden: [search]")
    v2 = "git:" + commit(policy, "balanced -> balanced-model-v2; forbid search")
    assert org.sync() == {"kind": "activated", "version": v2, "problems": []}
    calls_before = len(scenario.server.seen)

    dry = run_org_plan(scenario, "--dry-run", code=ExitCode.INVALID)
    assert '"policy": {"from": "' + v1 in dry.err  # the run followed the active version
    plan = dry.json()
    assert plan["policy_version"] == v2
    forbidden = {
        p["step"]
        for p in plan["preflight"]["problems"]
        if (p["check"], p["detail"], p["policy"])
        == (
            "policy",
            "tools: forbidden by org policy",
            "not search",
        )
    }
    assert forbidden == {"requirements", "baseline", "design"}, plan["preflight"]
    live = run_org_plan(scenario, "--run-id", "refused-at-v2", code=ExitCode.INVALID)
    assert "tools: forbidden by org policy (policy: not search)" in live.out
    assert len(scenario.server.seen) == calls_before  # refused before any model call
    assert org.api("GET", "/v1/runs/refused-at-v2", token).status_code == 404  # no slot taken

    # ── v3: `search` out of the agent types - invisible; routing applied again ─────────
    types = policy / "policy" / "agent-types.yaml"
    edit(types, "tools: [read_files, search, ", "tools: [read_files, ")
    v3 = "git:" + commit(policy, "take search out of the agent types")
    assert org.sync()["version"] == v3
    applied = org.apply_routing()
    assert applied.status == "applied" and applied.policy_version == v3
    assert org.tier_models()["balanced"] == gateway_model(BALANCED_V2)
    assert org.tier_models()["deep-reasoning"] == gateway_model(DEEP)  # unchanged

    plan = run_org_plan(scenario, "--dry-run").json()
    assert plan["policy_version"] == v3
    last = run_org_plan(scenario).json()
    assert last["status"] == "completed", last
    assert last["policy_version"] == v3  # the run record
    shown = scenario.ok("--json", "status", last["run_id"]).json()
    assert shown["policy_version"] == v3
    asked = calls_of(scenario, v3)
    assert asked
    for call in asked:
        assert "search" not in call["tools"]  # never offered, so never callable
        if call["tier"] == "balanced":
            assert call["model"] == gateway_model(BALANCED_V2)
    assert {c["step"] for c in asked if c["tier"] == "balanced"} == {"requirements", "baseline"}
    held = org.ok("GET", f"/v1/runs/{last['run_id']}", token)
    assert (held["policy_version"], held["policy_known"]) == (v3, True)
    assert scenario.finish_models() == []  # every recorded call used, none missing

    # ── the audit chain: each version activated, the gateway told about v1 and v3 ──────
    chain = org.audit()
    activated = [r["policy_version"] for r in chain if r["event"] == "policy.activated"]
    assert activated == [v1, v2, v3]
    routed = [r["policy_version"] for r in chain if r["event"] == "routing.applied"]
    assert routed == [v1, v3]  # v2's apply was superseded by v3 before the worker ran
    assert git(repo, "rev-parse", "HEAD") == main_head  # the runs worked in their worktrees
