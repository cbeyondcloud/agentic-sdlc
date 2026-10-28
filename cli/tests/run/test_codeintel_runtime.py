"""The run's code intelligence as the repo config chooses it (M2-43): preflight warns about a
domain graph that is not built, and the step wiring gets the domain-graph adapter."""

import shutil

import pytest

from agentic_cli import runtime
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons import domain_graph


def configure(repo, codeintel: str) -> None:
    config = repo / ".agentic" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"version: 1\nclassification: internal\ncodeintel: {codeintel}\n", "utf-8")


def allow_codeintel(policy_repo) -> None:
    types = policy_repo / "policy" / "agent-types.yaml"
    text = types.read_text("utf-8")  # the analyst (first type) may use codeintel
    types.write_text(text.replace("    tools: []\n", "    tools: [read_files, codeintel]\n", 1))


def decision():
    policy = Runtime(Workspace.load()).ws.policy()
    return runtime.decide_sandbox(runtime.machine_readiness(), "solo", policy.sandbox)


def test_a_domain_graph_that_is_not_built_is_a_preflight_warning(workspace, policy_repo):
    configure(workspace, "{adapter: domain-graph}")
    allow_codeintel(policy_repo)

    report = Runtime(Workspace.load()).preflight("deliver", live=False)

    found = [p for p in report.problems if p.check == "codeintel"]
    assert found and all(p.severity == "warning" for p in found), report.text()
    assert "domain graph is not built" in found[0].detail
    assert report.passed, report.text()


def test_an_invalid_codeintel_section_is_a_preflight_error_among_the_others(workspace, policy_repo):
    # DF-467: listed as a problem, so the other preflight problems are listed too
    configure(workspace, "{adapter: graphify}")

    report = Runtime(Workspace.load()).preflight("deliver", live=False)

    found = [p for p in report.problems if p.check == "codeintel"]
    assert [p.severity for p in found] == ["error"] and not report.passed
    assert "codeintel.adapter" in found[0].detail
    assert "code-review-graph, domain-graph or none" in found[0].detail


def test_the_run_itself_still_refuses_an_invalid_section(workspace):
    configure(workspace, "{adapter: graphify}")

    with pytest.raises(CliError) as refused:
        Runtime(Workspace.load())._wiring(decision())
    assert refused.value.code == ExitCode.INVALID


def test_a_domain_graph_that_does_not_parse_is_a_preflight_warning(workspace, policy_repo):
    # DF-467: a graph.jsonl that will not parse no longer passes as built
    configure(workspace, "{adapter: domain-graph}")
    allow_codeintel(policy_repo)
    graph = workspace / ".agentic" / "local" / "graph"
    graph.mkdir(parents=True)
    (graph / "graph.jsonl").write_text("{not json\n", encoding="utf-8")

    report = Runtime(Workspace.load()).preflight("deliver", live=False)

    found = [p for p in report.problems if p.check == "codeintel"]
    assert found and all(p.severity == "warning" for p in found), report.text()
    assert "cannot be read" in found[0].detail and report.passed


def test_adapter_none_gives_the_steps_no_code_intelligence(workspace):
    configure(workspace, "{adapter: none}")

    assert Runtime(Workspace.load())._wiring(decision()).codeintel_for is None


def test_the_steps_get_the_domain_graph_the_repo_names(workspace, tmp_path, monkeypatch):
    if not domain_graph.installer_available():
        pytest.skip("no Zyroflo installer to build the graph with (Zyroflo PR Z7)")
    estate = tmp_path / "pantry"
    shutil.copytree(domain_graph.fixture_estate("pantry"), estate)
    graph = tmp_path / "estate-graph"
    domain_graph.build_graph(estate / "workspace.toml", graph)
    configure(workspace, f"{{adapter: domain-graph, graph: '{graph.as_posix()}', component: x}}")
    monkeypatch.setattr(runtime.codeintel_index, "available", lambda: False)

    wiring = Runtime(Workspace.load())._wiring(decision())
    intel = wiring.codeintel_for(workspace, tmp_path / "data")

    assert intel.name == "domain-graph"
    assert intel.overview().startswith("Domain graph of workspace pantry")
