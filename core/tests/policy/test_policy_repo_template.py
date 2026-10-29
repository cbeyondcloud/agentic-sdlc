"""The org policy-repo template, `templates/policy-repo/` (M2-51; spec §4.3, D30, D33).

An org admin creates the org's policy repo from it, copies the framework's library of
agents, pipelines and skills in, and points the control plane at it. It must load,
name no vendor or model, keep `restricted` code away from personal skills and memory,
ship the org deny list, and pass the policy-repo gate (`agentic policy check`)."""

import shutil
from datetime import date
from pathlib import Path

import pytest
from test_standalone_policy import VENDORS

from agentic.definitions import DefinitionSet
from agentic.definitions.versions import Deprecations, Pins, Rollout
from agentic.policy import load_policy
from agentic.skills import load_catalog
from agentic.skills.gate import check_policy_repo
from agentic.yamlio import read_text

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "templates" / "policy-repo"
LIBRARY = REPO / "library"
TODAY = date(2026, 10, 24)


@pytest.fixture(scope="module")
def policy():
    return load_policy(TEMPLATE / "policy")


def with_library(tmp_path: Path) -> Path:
    """The template as an admin sets it up: plus the library's agents, pipelines, skills."""
    root = tmp_path / "org-policy"
    shutil.copytree(TEMPLATE, root)
    for name in ("agents", "pipelines"):
        shutil.copytree(LIBRARY / name, root / name, dirs_exist_ok=True)
    shutil.rmtree(root / "skills")
    shutil.copytree(LIBRARY / "skills", root / "skills")
    return root


def test_template_loads(policy):
    assert policy.default_classification in policy.classifications
    assert policy.allow_repo_pipelines is False  # D33: pipelines only from this repo
    assert policy.allowed_repo_patterns  # an org names its repos
    assert set(policy.teams) == {"payments"}
    assert policy.skill_licences is not None and "internal" in policy.skill_licences
    assert policy.skill_limits.max_skill_tokens is not None


def test_no_vendor_names():
    found = {
        path.relative_to(TEMPLATE).as_posix(): VENDORS.findall(read_text(path))
        for path in TEMPLATE.rglob("*")
        if path.is_file()
    }
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_template_shows_the_prepass_and_checkpoint_keys(policy):
    """DF-533, DF-315: admins find `review_prepass` (set to its defaults) and the commented
    `checkpoint_exclude` example in org.yaml."""
    text = read_text(TEMPLATE / "policy" / "org.yaml")
    assert "\nreview_prepass:\n" in text and "# checkpoint_exclude:" in text
    assert policy.review_prepass.enabled and not policy.review_prepass.online_vulnerability_lookup
    assert policy.checkpoint_exclude == []
    assert "# commit_override:" in text and policy.commit_override.allow == []  # M2-61


def test_tiers_are_placeholders(policy):
    for name, tier in policy.tiers.items():
        assert "change-me" in tier.model, name


def test_restricted_has_personal_skills_and_memory_off(policy):
    restricted = policy.classifications["restricted"]  # decision 89
    assert (restricted.personal_skills, restricted.personal_memory) == (False, False)
    assert restricted.allowed_tiers == ["local"]


def test_org_deny_list_has_audit_fix_rules(policy):
    """DF-277/DF-347: an org copying the library qa agents gets the audit-fix denies."""
    for rule in ("npm audit fix", "pip-audit --fix", "pip-audit -o|--output", "git push"):
        assert rule in policy.deny.shell, rule


def test_every_team_has_its_layer_and_a_codeowners_line(policy):
    owners = read_text(TEMPLATE / "CODEOWNERS")
    for team in policy.teams:
        layer = TEMPLATE / "teams" / team
        assert (layer / "pipelines" / "overlays").is_dir(), team
        assert load_catalog(layer / "skills").skills == {}, team
        assert f"/teams/{team}/ " in owners, team
        assert f"/policy/teams/{team}.yaml " in owners, team


def test_template_definitions_discover(policy):
    found = DefinitionSet.discover(org=TEMPLATE, team=TEMPLATE / "teams" / "payments", repo=None)
    assert found.pipelines == {} and found.agents == {}  # the library is copied in later


def test_the_skills_catalog_loads_and_shows_the_plugin_opt_in():
    catalog = load_catalog(TEMPLATE / "skills")
    assert catalog.skills == {} and catalog.plugins == {}
    text = read_text(TEMPLATE / "skills" / "catalog.yaml")
    assert "# plugins:" in text and "visual-companion" in text


def test_the_gate_passes_the_template_with_the_library(tmp_path: Path):
    assert check_policy_repo(with_library(tmp_path)) == []


def test_the_gate_uses_the_org_skill_licences(tmp_path: Path):
    """DF-367: the licences and limits come from org policy, not the catalog under review."""
    root = with_library(tmp_path)
    catalog = root / "skills" / "catalog.yaml"
    text = read_text(catalog).replace("licence: MIT", "licence: GPL-3.0-only", 1)
    catalog.write_text(text.replace("licences: [", "licences: [GPL-3.0-only, ", 1), "utf-8")
    problems = check_policy_repo(root)
    assert problems and any("GPL-3.0-only" in p and "org policy" in p for p in problems)

    licences = root / "policy" / "licences.yaml"
    allowed = read_text(licences).replace("skills: [", "skills: [GPL-3.0-only, ", 1)
    licences.write_text(allowed, "utf-8")
    remaining = check_policy_repo(root)
    assert not any("org policy" in p for p in remaining), remaining


def test_the_gate_names_a_broken_team_catalog(tmp_path: Path):
    root = with_library(tmp_path)
    (root / "teams" / "payments" / "skills" / "catalog.yaml").write_text(
        "skills: [not a mapping\n", "utf-8"
    )
    problems = check_policy_repo(root)
    assert any(p.startswith("teams/payments/skills/catalog.yaml") for p in problems), problems


def test_template_rollout_loads(policy):
    """M2-15: the example rollout file loads with nothing rolled out (its examples are
    comments: a live entry needs an archived previous version)."""
    assert policy.rollout == Rollout() and policy.deprecated == Deprecations()
    assert policy.teams["payments"].pins == Pins()
    text = read_text(TEMPLATE / "policy" / "rollout.yaml")
    for needed in ("previous:", "teams:", "blocked_from:", "versions/"):
        assert needed in text, needed
    assert "# pins:" in read_text(TEMPLATE / "policy" / "teams" / "payments.yaml")


def test_template_overlay_applies_to_the_library_pipeline(policy, tmp_path: Path):
    """M2-23: the example overlay tightens the library's feature-delivery for one
    repository of team payments, keeping every org-required gate."""
    root = with_library(tmp_path)
    overlay = root / "teams" / "payments" / "pipelines" / "overlays" / "payments-api.yaml"
    text = read_text(overlay)
    example = text[text.index("# pipelines:") :]  # the commented example, uncommented
    overlay.write_text(
        text.replace(example, "".join(line[2:] for line in example.splitlines(True))),
        encoding="utf-8",
    )
    assert {"requirements-approval", "review-approval"} <= set(
        policy.required_steps["feature-delivery"]
    )
    rules = policy.rules(team="payments", repo="git.example.com/your-org/payments-api", today=TODAY)
    found = DefinitionSet.discover(
        org=root, team=root / "teams" / "payments", repo=None, rules=rules
    )
    applied = found.overlays["feature-delivery"]
    assert applied.file.name == "payments-api.yaml"
    assert "gate review-approval four_eyes on" in applied.notes
    other = policy.rules(team="payments", repo="git.example.com/your-org/billing", today=TODAY)
    plain = DefinitionSet.discover(
        org=root, team=root / "teams" / "payments", repo=None, rules=other
    )
    assert plain.overlays == {}


def _overlay(root: Path, text: str) -> None:
    path = root / "teams" / "payments" / "pipelines" / "overlays" / "payments-api.yaml"
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("pipeline", "removed"),
    [
        ("feature-delivery", "[build-review]"),
        ("feature-delivery", "[develop]"),
        ("feature-delivery", "[review, qa]"),
        ("feature-delivery", "[requirements]"),
        ("bugfix", "[fix-review]"),
        ("bugfix", "[qa]"),
    ],
)
def test_the_template_keeps_the_build_loop_from_overlays(tmp_path: Path, pipeline, removed):
    """Review F1: with the template's required_steps, an overlay cannot drop the work the
    required gates approve - the developer, code review and QA (spec §6.1) - and leave a
    pull request nobody built, reviewed or tested."""
    root = with_library(tmp_path)
    _overlay(root, f"pipelines:\n  {pipeline}:\n    remove: {removed}\n")
    problems = check_policy_repo(root)
    assert any(
        p.startswith("teams/payments/pipelines/overlays/payments-api.yaml: ")
        and ("rule required-step" in p or "rule required-gate" in p)
        for p in problems
    ), problems


def test_the_gate_catches_an_overlay_that_breaks_the_pipeline(tmp_path: Path):
    """Review F2: an overlay that leaves a loop's `until` reading a removed step's handoff
    passes the overlay rules (nothing it removes is required here) but fails every laptop's
    preflight - so the policy-repo gate refuses it, naming the overlay."""
    root = with_library(tmp_path)
    org = root / "policy" / "org.yaml"
    text = read_text(org)
    start, end = text.index("required_steps:"), text.index("# Where `agentic init`")
    org.write_text(text[:start] + "required_steps: {}\n\n" + text[end:], encoding="utf-8")
    _overlay(root, "pipelines:\n  feature-delivery:\n    remove: [review, qa]\n")
    problems = check_policy_repo(root)
    assert any(
        p.startswith("teams/payments/pipelines/overlays/payments-api.yaml: ")
        and "feature-delivery" in p
        and "reads 'review', which no earlier step produces" in p
        for p in problems
    ), problems


def test_readme_covers_codeowners_branch_protection_and_bootstrap():
    text = read_text(TEMPLATE / "README.md")
    for needed in (
        "CODEOWNERS",
        "branch protection",
        "Require review from Code Owners",
        "agentic policy check",
        "teams/<team>/",
        "set-policy-repo",
        "library/agents",
        "backend_plugins",
        "restricted",
        "agentic skills submit",
        "pipelines/overlays/<repo>.yaml",
        "required_steps",
        "re-anchor",
        "rollout.yaml",
        "pins:",
        "Resolution order",
    ):
        assert needed in text, needed
