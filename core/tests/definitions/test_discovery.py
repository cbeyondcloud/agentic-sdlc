"""Tests for finding agent and pipeline files across the org, team and repo layers."""

from pathlib import Path

import pytest

from agentic.definitions import DefinitionError, DefinitionSet, UnknownDefinitionError

JIRA_FIELD = (
    "fields:\n  jira_ticket:\n    type: string\n    required: true\n"
    "    description: The Jira issue\n    example: PAY-123\n"
)
ROLLBACK_SECTION = "sections:\n  - heading: Rollback plan\n    guidance: How to undo it.\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def make_layer(
    root: Path,
    agents: dict[str, str] | None = None,
    pipelines: tuple[str, ...] = (),
    agent_produces: str | None = None,
) -> Path:
    """Create a layer folder: agents/<name>.md (description given) and pipelines/<name>.yaml;
    with `agent_produces`, also agents/writer.md producing that handoff kind."""
    root.mkdir(parents=True, exist_ok=True)
    if agent_produces:
        write(
            root / "agents" / "writer.md",
            "---\nname: writer\ntype: general\nversion: 1.0.0\ndescription: writes\n"
            f"produces: {agent_produces}\n---\nWrite it.\n",
        )
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


def test_pipeline_overlays_folder_is_not_a_pipeline(tmp_path: Path):
    """D33: per-repo overlays live in `teams/<team>/pipelines/overlays/` (loaded by M2-23);
    discovery skips that folder, and any other folder there is still an error."""
    team = make_layer(tmp_path / "team", agents={"developer": "team"})
    write(team / "pipelines" / "overlays" / "app.yaml", "steps: []\n")
    found = DefinitionSet.discover(org=None, team=team, repo=None)
    assert "app" not in found.pipelines
    write(team / "pipelines" / "drafts" / "x.yaml", "name: x\n")
    with pytest.raises(DefinitionError, match="drafts"):
        DefinitionSet.discover(org=None, team=team, repo=None)


# ── handoff extensions per layer (D37) ────────────────────────────────────────


def test_handoff_extensions_stack_across_layers(tmp_path: Path):
    org, team, repo = (make_layer(tmp_path / name) for name in ("org", "team", "repo"))
    write(org / "handoffs" / "review.yaml", JIRA_FIELD)
    write(repo / "handoffs" / "review.yaml", ROLLBACK_SECTION)

    review = DefinitionSet.discover(org=org, team=team, repo=repo).handoffs.schema("review")
    assert "jira_ticket" in review.model.model_fields
    assert review.sections[-2].heading == "Rollback plan"


def test_without_extensions_the_builtin_catalog_is_used(tmp_path: Path):
    from agentic.handoffs.schema import BUILTIN_CATALOG

    found = DefinitionSet.discover(org=None, team=None, repo=make_layer(tmp_path / "repo"))
    assert found.handoffs.digest == BUILTIN_CATALOG.digest


def test_a_repo_cannot_define_a_new_kind(tmp_path: Path):
    repo = make_layer(tmp_path / "repo")
    write(repo / "handoffs" / "threat-model.yaml", "new_kind: true\ntitle: T\n")

    with pytest.raises(DefinitionError, match="policy repo"):
        DefinitionSet.discover(org=None, team=None, repo=repo)


def test_only_yaml_files_in_a_handoffs_folder(tmp_path: Path):
    repo = make_layer(tmp_path / "repo")
    write(repo / "handoffs" / "review.yml", ROLLBACK_SECTION)

    with pytest.raises(DefinitionError, match="only \\*.yaml"):
        DefinitionSet.discover(org=None, team=None, repo=repo)


def test_an_agent_producing_an_unknown_kind_is_refused(tmp_path: Path):
    repo = make_layer(tmp_path / "repo", agent_produces="threat-model")

    with pytest.raises(DefinitionError, match="unknown handoff kind 'threat-model'"):
        DefinitionSet.discover(org=None, team=None, repo=repo)


def test_a_kind_defined_by_the_team_can_be_produced(tmp_path: Path):
    team = make_layer(tmp_path / "team")
    write(team / "handoffs" / "threat-model.yaml", "new_kind: true\ntitle: Threat model\n")
    repo = make_layer(tmp_path / "repo", agent_produces="threat-model")

    found = DefinitionSet.discover(org=None, team=team, repo=repo)
    assert found.handoffs.schema("threat-model").title == "Threat model"


# ── M1-81 hardening (DF-03) ──────────────────────────────────────────────────


def test_an_agent_file_must_be_named_after_its_agent(tmp_path: Path):
    """reviewer.md defining `name: developer` would silently replace developer.md (or the
    org's developer) by sort order: refused, naming the file and both names (DF-03)."""
    repo = make_layer(tmp_path / "repo", agents={"developer": "the developer"})
    write(
        repo / "agents" / "reviewer.md",
        "---\nname: developer\ntype: general\nversion: 1.0.0\ndescription: copy\n---\nDo it.\n",
    )

    with pytest.raises(DefinitionError) as error:
        DefinitionSet.discover(org=None, team=None, repo=repo)

    assert error.value.path == repo / "agents" / "reviewer.md"
    assert "reviewer.md defines agent 'developer'" in str(error.value)


def test_an_agent_file_name_is_compared_exactly(tmp_path: Path):
    """Developer.md for `name: developer` is a different file name on Linux CI than on a
    Windows or macOS laptop, so the case must match too (DF-03)."""
    repo = make_layer(tmp_path / "repo")
    write(
        repo / "agents" / "Developer.md",
        "---\nname: developer\ntype: general\nversion: 1.0.0\ndescription: d\n---\nDo it.\n",
    )

    with pytest.raises(DefinitionError, match="defines agent 'developer'") as error:
        DefinitionSet.discover(org=None, team=None, repo=repo)

    # `set name: Developer` would be refused by the name rule: not offered (DF-323)
    assert "set name: Developer" not in str(error.value)
    assert "developer.md" in str(error.value)


def test_a_valid_stem_is_offered_as_the_name(tmp_path: Path):  # DF-323
    repo = make_layer(tmp_path / "repo")
    write(
        repo / "agents" / "reviewer.md",
        "---\nname: developer\ntype: general\nversion: 1.0.0\ndescription: d\n---\nDo it.\n",
    )

    with pytest.raises(DefinitionError, match="set name: reviewer"):
        DefinitionSet.discover(org=None, team=None, repo=repo)
