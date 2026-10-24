"""The standalone policy repo `agentic init --standalone` installs (M1-62, DF-217, DF-224):
the template's policy, the library agents, pipelines and skills (with the template's
preload rule applied), and a starter file per agent type."""

from pathlib import Path

import pytest
from cli_helpers import ROOT

from agentic.definitions import DefinitionSet, load_agent
from agentic.policy import load_policy
from agentic.skills import PreloadRule, load_catalog
from agentic.skills.install import sync_skills
from agentic.yamlio import load_yaml, read_text
from agentic_cli import standalone
from agentic_cli.errors import CliError

LIBRARY_AGENTS = {
    "business-analyst",
    "code-review",
    "design",
    "developer",
    "qa",
    "qa-baseline",
    "learning-analyst",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    return standalone.assemble(tmp_path_factory.mktemp("standalone") / "repo")


def test_the_policy_loads(built: Path):
    policy = load_policy(built / "policy")
    assert "restricted" in policy.classifications and "local" in policy.tiers
    assert (built / "README.md").is_file()


def test_library_agents_and_pipelines_are_the_org_layer(built: Path):
    found = DefinitionSet.discover(built, None, None, allow_repo_pipelines=True)
    assert set(found.agents) == LIBRARY_AGENTS
    assert {"feature-delivery", "bugfix"} <= set(found.pipelines)


def test_the_catalog_has_the_template_preload_rule_and_every_skill_verifies(
    built: Path, tmp_path: Path
):
    catalog = load_catalog(built / "skills")
    data, _ = load_yaml(read_text(ROOT / "templates/standalone/skills/preload.yaml"))
    assert catalog.preload_for == PreloadRule.model_validate(data["preload_for"])
    assert catalog.skills == load_catalog(ROOT / "library" / "skills").skills
    report = sync_skills(catalog, built / "skills", tmp_path / "installed", list(catalog.skills))
    assert set(report.installed) == set(catalog.skills)


def test_one_starter_template_per_library_agent_type(built: Path):
    types = {load_agent(ROOT / "library/agents" / f"{n}.md").type for n in LIBRARY_AGENTS}
    found = {path.stem for path in (built / "templates" / "agents").glob("*.md")}
    assert found == types
    starter = load_agent(built / "templates" / "agents" / "qa.md")
    assert starter.name == "qa"  # the type's own agent, not qa-baseline
    assert set(types) <= set(load_policy(built / "policy").agent_types)


def test_files_are_written_with_lf_line_ends(built: Path):
    assert b"\r\n" not in (built / "skills" / "catalog.yaml").read_bytes()


def test_a_missing_framework_root_is_an_error_with_a_hint(tmp_path: Path):
    with pytest.raises(CliError) as caught:
        standalone.assemble(tmp_path / "out", root=tmp_path / "nowhere")
    assert "AGENTIC_FRAMEWORK_ROOT" in (caught.value.hint or "")
    assert not (tmp_path / "out").exists()


def test_the_framework_root_can_be_set(tmp_path: Path):
    assert standalone.framework_root({"AGENTIC_FRAMEWORK_ROOT": str(tmp_path)}) == tmp_path
    assert standalone.framework_root({}) == ROOT
