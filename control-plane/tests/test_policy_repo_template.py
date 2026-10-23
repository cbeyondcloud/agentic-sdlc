"""The org policy-repo template (M2-51) against the control plane: its validator accepts
the template, its CODEOWNERS gives `pipelines/` to the org admins and `teams/<team>/` to
that team's leads (D33), and a fresh tenant's policy sync activates it."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from codeowners import CodeOwners
from policy_repos import REPO_ROOT, commit, git, head, url
from test_policy_sync import _active, _point, _sync, settings  # noqa: F401 - fixture

from agentic_cp.policy_sync.validate import validate_policy_tree

TEMPLATE = REPO_ROOT / "templates" / "policy-repo"
VERSION = "git:" + "c" * 40
ADMINS = "@your-org/agentic-org-admins"
LEADS = "@your-org/payments-leads"


def owners(path: str) -> list[str]:
    rules = CodeOwners((TEMPLATE / "CODEOWNERS").read_text(encoding="utf-8"))
    return [name for _, name in rules.of(path)]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pipelines/feature-delivery.yaml", [ADMINS]),
        ("policy/tiers.yaml", [ADMINS]),
        ("skills/catalog.yaml", [ADMINS]),
        ("agents/developer.md", [ADMINS]),
        ("CODEOWNERS", [ADMINS]),
        ("teams/payments/pipelines/feature-delivery.yaml", [LEADS]),
        ("teams/payments/pipelines/overlays/app.yaml", [LEADS]),
        ("teams/payments/skills/catalog.yaml", [LEADS]),
        ("policy/teams/payments.yaml", [LEADS, ADMINS]),
    ],
)
def test_codeowners_maps_pipelines_to_org_admins_and_teams_to_leads(path, expected):
    assert owners(path) == expected


def test_template_validates_with_the_control_plane_validator(tmp_path: Path):
    root = tmp_path / "tree"
    shutil.copytree(TEMPLATE, root)
    problems, policy = validate_policy_tree(root, VERSION)
    assert problems == [] and policy is not None
    assert sorted(policy.teams) == ["payments"]


def test_a_broken_team_skills_catalog_is_named(tmp_path: Path):
    root = tmp_path / "tree"
    shutil.copytree(TEMPLATE, root)
    catalog = root / "teams" / "payments" / "skills" / "catalog.yaml"
    catalog.write_text("skills: [not a mapping\n", encoding="utf-8")
    problems, _ = validate_policy_tree(root, VERSION)
    assert any(p.startswith("teams/payments/skills/catalog.yaml") for p in problems), problems


def template_repo(path: Path) -> Path:
    """A git policy repo holding exactly the template's files, LF, on `main`."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for source in sorted(TEMPLATE.rglob("*")):
        if source.is_file():
            target = path / source.relative_to(TEMPLATE)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    commit(path, "policy repo from the template")
    return path


@pytest.mark.anyio
async def test_template_syncs_on_a_fresh_tenant(api, settings, tmp_path):  # noqa: F811
    repo = template_repo(tmp_path / "org-policy")
    await _point(api.db, api.a.id, url(repo))
    outcome = await _sync(api.db, settings, api.a.id)
    assert outcome.kind == "activated", outcome.problems
    active = await _active(api.db, api.a.id)
    assert active.version == "git:" + head(repo)
