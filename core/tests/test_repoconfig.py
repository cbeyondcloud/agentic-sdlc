"""Tests for the committed repo settings, `.agentic/config.yaml` (M1-52)."""

import pytest

from agentic.repoconfig import RepoConfig, RepoConfigError, load_repo_config, render_repo_config


def test_a_minimal_config_needs_only_a_classification():
    config = load_repo_config("version: 1\nclassification: internal\n")
    assert config.classification == "internal"
    assert config.team is None and config.pipelines == [] and config.components == []
    assert config.codeintel.adapter == "code-review-graph"


def test_the_domain_graph_adapter_takes_a_graph_folder_and_a_component():
    config = load_repo_config(
        "version: 1\nclassification: internal\n"
        "codeintel: {adapter: domain-graph, graph: ../estate-graph, component: pantry-api}\n"
    )
    assert config.codeintel.adapter == "domain-graph"
    assert config.codeintel.graph == "../estate-graph"
    assert config.codeintel.component == "pantry-api"


def test_the_domain_graph_defaults_to_the_local_graph_folder_and_the_workspace_root():
    config = load_repo_config("version: 1\nclassification: internal\n")
    assert config.codeintel.graph is None
    assert config.codeintel.graph_folder == ".agentic/local/graph"
    assert config.codeintel.component is None
    assert "graph:" not in render_repo_config(config)  # an unset default is not written


def test_render_then_load_round_trips():
    config = RepoConfig(
        classification="restricted",
        team="payments",
        pipelines=["feature-delivery"],
        components=["libs/shared"],
    )
    text = render_repo_config(config)
    assert "\r" not in text and text.endswith("\n")
    assert load_repo_config(text) == config


@pytest.mark.parametrize(
    "text, problem",
    [
        ("version: 1\n", "classification"),
        ("version: 2\nclassification: internal\n", "version"),
        ("version: 1\nclassification: internal\nclasification: x\n", "clasification"),
        ("- a\n", "mapping"),
        ("version: 1\nclassification: internal\nteam: Not A Slug\n", "team"),
        ("version: 1\nclassification: internal\ncomponents: ['../up']\n", "components"),
        ("version: 1\nclassification: internal\ncodeintel: {adapter: graphify}\n", "adapter"),
        ("version: 1\nclassification: internal\ncodeintel: {graph: ''}\n", "graph"),
        ("version: 1\nclassification: internal\ncodeintel: {component: ''}\n", "component"),
        ("version: 1\nclassification: internal\ncodeintel: {grahp: x}\n", "grahp"),
    ],
)
def test_invalid_configs_are_refused_with_the_field(text: str, problem: str):
    with pytest.raises(RepoConfigError) as raised:
        load_repo_config(text)
    assert problem in str(raised.value)
