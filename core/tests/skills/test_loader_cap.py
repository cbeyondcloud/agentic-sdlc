"""Skills beyond the pre-load cap are recorded, never dropped silently (DF-144)."""

from pathlib import Path

from _skill_helpers import entry, make_skill, write_catalog

from agentic.skills import load_catalog, skill_digest
from agentic.skills.install import sync_skills
from agentic.skills.loader import MAX_PRELOADED, InstalledSkills, skill_prompt

KEYS = [f"acme/skill-{n}" for n in range(MAX_PRELOADED + 2)]


def installed(tmp_path: Path, events: list) -> InstalledSkills:
    root = tmp_path / "catalog"
    entries = {key: entry(skill_digest(make_skill(root, key))) for key in KEYS}
    write_catalog(root, entries, preload_for={"tiers": ["balanced"]})
    catalog = load_catalog(root)
    sync_skills(catalog, root, tmp_path / "dest", KEYS)
    return InstalledSkills(catalog, tmp_path / "dest", record=lambda e, f: events.append((e, f)))


def test_skills_beyond_the_cap_are_recorded_for_an_agent_without_the_tool(tmp_path, resolved):
    events: list = []
    library = installed(tmp_path, events)
    agent = resolved.model_copy(update={"skills": KEYS, "tools": ["read_files"]})
    prompt = skill_prompt(agent, library, preload_all=False)
    assert prompt.count("# Skill: ") == MAX_PRELOADED
    truncated = [f["skill"] for e, f in events if e == "skills.truncated"]
    assert truncated == KEYS[MAX_PRELOADED:]


def test_with_the_tool_the_rest_are_listed_not_truncated(tmp_path, resolved):
    events: list = []
    library = installed(tmp_path, events)
    agent = resolved.model_copy(update={"skills": KEYS, "tools": ["activate_skill"]})
    prompt = skill_prompt(agent, library, preload_all=True)
    assert all(f"- {key}:" in prompt for key in KEYS[MAX_PRELOADED:])
    assert not [e for e, _ in events if e == "skills.truncated"]


def test_preloads_all_follows_the_catalog_rule(tmp_path, resolved):
    library = installed(tmp_path, [])
    assert library.preloads_all(resolved.model_copy(update={"tier": "balanced"}))
    assert not library.preloads_all(resolved.model_copy(update={"tier": "fast"}))
