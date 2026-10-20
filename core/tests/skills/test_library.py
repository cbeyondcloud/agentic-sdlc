"""Tests for the shipped default skills in `library/skills/` (M1-40)."""

import re
from pathlib import Path

import pytest

from agentic.definitions import load_agent
from agentic.policy import ResolveContext, load_policy, resolve_agent
from agentic.skills import load_catalog, skill_folder
from agentic.skills.admission import admit_catalog, telemetry_urls
from agentic.skills.install import sync_skills
from agentic.skills.loader import InstalledSkills, estimate_tokens
from agentic.skills.select import select_skills

LIBRARY = Path(__file__).resolve().parents[3] / "library" / "skills"
# Spec §11.4 and the master plan's "never downloaded for the native loop" list.
EXCLUDED = {
    "using-superpowers",
    "subagent-driven-development",
    "executing-plans",
    "using-git-worktrees",
    "finishing-a-development-branch",
    "caveman",
    "docx",
    "pdf",
    "pptx",
    "xlsx",
}


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(LIBRARY)


def test_library_catalog_validates_and_every_skill_is_admitted(catalog):
    assert len(catalog.skills) >= 10
    problems = {
        key: [f"{r.check} {r.path}: {r.reason}" for r in found]
        for key, found in admit_catalog(LIBRARY).items()
        if found
    }
    assert problems == {}


def test_excluded_skills_absent(catalog):
    assert EXCLUDED <= set(catalog.excluded)
    names = {key.split("/")[1] for key in catalog.skills}
    folders = {p.name for p in LIBRARY.glob("*/*") if p.is_dir()}
    assert not EXCLUDED & names
    assert not EXCLUDED & folders


def test_no_telemetry_urls():
    found = {}
    for path in LIBRARY.rglob("*"):
        if path.is_file():
            if urls := telemetry_urls(path.read_text(encoding="utf-8")):
                found[path.relative_to(LIBRARY).as_posix()] = urls
    assert found == {}


def test_every_source_has_a_notice_entry(catalog):
    notice = (LIBRARY / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for key, entry in catalog.skills.items():
        if entry.source == "internal":
            continue
        assert entry.source in notice, key
        assert entry.commit in notice, key
        assert f"`{key}`" in notice, key
        assert entry.licence in notice, key
        licence = (skill_folder(LIBRARY, key) / "LICENSE.txt").read_text(encoding="utf-8")
        assert re.search(r"copyright", licence, re.IGNORECASE), key


def test_authoring_skills_are_for_admins_only(catalog):
    authoring = {"superpowers/writing-skills", "anthropics/skill-creator"}
    for key in authoring & set(catalog.skills):
        assert catalog.skills[key].audience == "admins"
    assert any(entry.audience == "admins" for entry in catalog.skills.values())


def test_default_role_skills_resolve(catalog, fixtures: Path):
    """The fixture policy's agent types name skills the default catalog provides."""
    policy = load_policy(fixtures / "policy" / "policy")
    for agent_type in policy.agent_types.values():
        for skill in agent_type.skills:
            assert skill in catalog.skills, skill


def test_default_catalog_end_to_end(catalog, fixtures: Path, tmp_path: Path):
    """Select for a developer, install, and build the developer's skill block."""
    policy = load_policy(fixtures / "policy" / "policy")
    developer = load_agent(fixtures / "agents" / "developer.md")
    resolved, violations = resolve_agent(developer, policy, ResolveContext(team="payments"))
    assert resolved is not None, violations
    resolved = resolved.model_copy(update={"tools": [*resolved.tools, "activate_skill"]})
    chosen = select_skills(catalog, resolved.skills, roles=["developer"])
    assert chosen.missing == () and "superpowers/writing-skills" not in chosen.names
    dest = tmp_path / ".agentic" / "local" / "skills"
    sync_skills(catalog, LIBRARY, dest, chosen.names)
    skills = InstalledSkills(catalog, dest)
    prompt = skills.prompt_for(resolved)
    assert "# Skill: superpowers/test-driven-development" in prompt  # loading: pre
    assert "- superpowers/systematic-debugging: " in prompt  # on-demand: one catalog line
    for key in chosen.names:
        assert estimate_tokens(skills.catalog_line(key)) <= 100


def test_pre_loaded_skills_per_role_stay_within_the_cap(catalog):
    """Spec §11.3: at most 3-5 pre-loaded skills per role; the catalog marks few as pre."""
    pre = [key for key, entry in catalog.skills.items() if entry.loading == "pre"]
    assert 0 < len(pre) <= 10
