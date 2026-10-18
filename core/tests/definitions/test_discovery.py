"""Tests for finding agent and pipeline files across the org, team and repo layers."""

from pathlib import Path

import pytest

from agentic.definitions import DefinitionError, DefinitionSet, UnknownDefinitionError


def make_layer(
    root: Path, agents: dict[str, str] | None = None, pipelines: tuple[str, ...] = ()
) -> Path:
    """Create a layer folder: agents/<name>.md (description given) and pipelines/<name>.yaml."""
    for name, description in (agents or {}).items():
        path = root / "agents" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ntype: general\nversion: 1.0.0\ndescription: {description}\n"
            "---\nDo it.\n",
            encoding="utf-8",
        )
    for name in pipelines:
        path = root / "pipelines" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"name: {name}\nversion: 1.0.0\nsteps:\n  - id: s1\n    agent: developer\n",
            encoding="utf-8",
        )
    return root


def test_repo_definition_overrides_org(tmp_path: Path):
    org = make_layer(tmp_path / "org", agents={"developer": "org version", "qa": "org qa"})
    repo = make_layer(tmp_path / "repo", agents={"developer": "repo version"})

    found = DefinitionSet.discover(org=org, team=None, repo=repo)

    assert found.agent("developer").description == "repo version"
    assert found.agent_source("developer") == "repo"
    assert found.agent("qa").description == "org qa"  # only in org: still found
    assert found.agent_source("qa") == "org"


def test_team_overrides_org_and_repo_overrides_team(tmp_path: Path):
    org = make_layer(tmp_path / "org", agents={"a": "org", "b": "org"})
    team = make_layer(tmp_path / "team", agents={"a": "team", "b": "team"})
    repo = make_layer(tmp_path / "repo", agents={"b": "repo"})

    found = DefinitionSet.discover(org=org, team=team, repo=repo)

    assert (found.agent("a").description, found.agent_source("a")) == ("team", "team")
    assert (found.agent("b").description, found.agent_source("b")) == ("repo", "repo")


def test_repo_pipelines_ignored_unless_allowed(tmp_path: Path):
    org = make_layer(tmp_path / "org", pipelines=("feature-delivery",))
    repo = make_layer(tmp_path / "repo", pipelines=("my-repo-pipeline",))

    locked = DefinitionSet.discover(org=org, team=None, repo=repo)  # default: not allowed
    allowed = DefinitionSet.discover(org=org, team=None, repo=repo, allow_repo_pipelines=True)

    assert locked.pipeline_names() == ["feature-delivery"]
    assert any("my-repo-pipeline" in warning for warning in locked.warnings)
    assert allowed.pipeline_names() == ["feature-delivery", "my-repo-pipeline"]
    assert allowed.pipeline_source("my-repo-pipeline") == "repo"


def test_unknown_agent_name_raises(tmp_path: Path):
    found = DefinitionSet.discover(org=make_layer(tmp_path / "org"), team=None, repo=None)

    with pytest.raises(UnknownDefinitionError) as error:
        found.agent("ghost")

    assert "ghost" in str(error.value)


@pytest.mark.parametrize("bad", ["agents/reviewer.yml", "pipelines/ship.yml", "agents/notes.txt"])
def test_unexpected_file_in_layer_rejected(tmp_path: Path, bad: str):
    org = make_layer(tmp_path / "org", agents={"developer": "org"})
    (org / bad).parent.mkdir(parents=True, exist_ok=True)
    (org / bad).write_text("name: x\n", encoding="utf-8")

    with pytest.raises(DefinitionError) as error:
        DefinitionSet.discover(org=org, team=None, repo=None)

    assert bad.split("/")[1] in str(error.value)
