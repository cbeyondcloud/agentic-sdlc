"""The standalone policy repo `agentic init --standalone` installs (M1-62, DF-217, DF-224):
the template's policy, the library agents, pipelines and skills (with the template's
preload rule applied), and a starter file per agent type."""

import shutil
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


def _framework(folder: Path) -> Path:
    (folder / "templates" / "standalone").mkdir(parents=True)
    return folder


def test_framework_root_prefers_env_then_checkout_then_bundle(tmp_path: Path, monkeypatch):
    """An installed wheel has no checkout: it uses the template and library the release
    bundles into agentic_cli/_framework (DF-138, DF-188, DF-341)."""
    checkout, bundle = tmp_path / "checkout", _framework(tmp_path / "bundle")
    checkout.mkdir()
    monkeypatch.setattr(standalone, "_CHECKOUT", checkout)
    monkeypatch.setattr(standalone, "BUNDLED", bundle)
    assert standalone.framework_root({}) == bundle  # no checkout: the bundle
    _framework(checkout)
    assert standalone.framework_root({}) == checkout  # a dev checkout wins
    given = {"AGENTIC_FRAMEWORK_ROOT": str(tmp_path / "mine")}
    assert standalone.framework_root(given) == tmp_path / "mine"  # the env wins
    monkeypatch.setattr(standalone, "BUNDLED", tmp_path / "absent")
    (checkout / "templates" / "standalone").rmdir()
    assert standalone.framework_root({}) == checkout  # neither: the error names it


def test_bundled_skills_follow_the_framework_root(tmp_path: Path, monkeypatch):
    from agentic_cli import dev_init

    monkeypatch.setattr(standalone, "_CHECKOUT", tmp_path / "checkout")
    monkeypatch.setattr(standalone, "BUNDLED", _framework(tmp_path / "bundle"))
    monkeypatch.delenv("AGENTIC_FRAMEWORK_ROOT", raising=False)
    assert dev_init.bundled_skills() == tmp_path / "bundle" / "library" / "skills"
    monkeypatch.setattr(standalone, "_CHECKOUT", ROOT)
    assert dev_init.bundled_skills() == ROOT / "library" / "skills"


def _copy_framework(folder: Path) -> Path:
    for rel in ("templates/standalone", "library/agents", "library/pipelines", "library/skills"):
        shutil.copytree(ROOT / rel, folder / rel)
    return folder


def test_refresh_takes_the_template_and_keeps_the_persons_policy(tmp_path: Path):
    """DF-273: the new template replaces the library copy; every policy/ file the person
    has is carried over; the result names what the template changed."""
    installed = standalone.assemble(tmp_path / "installed")
    tiers = installed / "policy" / "tiers.yaml"
    tiers.write_text(tiers.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    (installed / "policy" / "extra.yaml").write_text("mine: true\n", encoding="utf-8")
    newer = _copy_framework(tmp_path / "framework")
    developer = newer / "library" / "agents" / "developer.md"
    developer.write_text(developer.read_text(encoding="utf-8") + "\nNewer.\n", "utf-8")
    (newer / "library" / "agents" / "qa-baseline.md").unlink()
    (newer / "library" / "pipelines" / "extra.yaml").write_bytes(
        (newer / "library" / "pipelines" / "bugfix.yaml").read_bytes()
    )

    done = standalone.refresh(installed, tmp_path / "new", newer)

    new = tmp_path / "new"
    assert (new / "policy" / "tiers.yaml").read_text(encoding="utf-8").endswith("# mine\n")
    assert (new / "policy" / "extra.yaml").read_text(encoding="utf-8") == "mine: true\n"
    assert "Newer." in (new / "agents" / "developer.md").read_text(encoding="utf-8")
    assert not (new / "agents" / "qa-baseline.md").exists()
    assert done.kept == ("policy/extra.yaml", "policy/tiers.yaml")
    assert "agents/developer.md" in done.changed
    assert "agents/qa-baseline.md" in done.removed
    assert "pipelines/extra.yaml" in done.added
    assert done.template_changed
    assert installed.is_dir()  # refresh never touches the installed copy itself


def test_refresh_with_the_same_template_changes_nothing(tmp_path: Path):
    installed = standalone.assemble(tmp_path / "installed")
    done = standalone.refresh(installed, tmp_path / "new")
    assert not done.template_changed and done.kept == ()
