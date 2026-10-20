"""Preflight refuses an agent whose skills are missing or cannot all be pre-loaded
(DF-136, DF-144)."""

from pathlib import Path

import pytest
from _engine_support import AGENTS, write

from agentic.definitions import DefinitionSet, load_agent
from agentic.engine.preflight import preflight, skill_problems
from agentic.policy import ResolveContext, load_policy, resolve_agent
from agentic.skills.loader import MAX_PRELOADED

PIPELINE = "name: test\nversion: 1.0.0\nsteps:\n  - id: develop\n    agent: dev\n"


class Skills:
    def __init__(self, missing=(), preload_all=False) -> None:
        self.missing, self.preload_all = set(missing), preload_all

    def unavailable(self, name: str) -> str | None:
        return "not installed" if name in self.missing else None

    def preloads_all(self, agent) -> bool:
        return self.preload_all


@pytest.fixture
def setup(tmp_path: Path, fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    layer = tmp_path / "layer"
    for name, extra in AGENTS.items():
        write(
            layer / "agents" / f"{name}.md",
            f"---\nname: {name}\n{extra}version: 1.0.0\ndescription: {name}\n---\nGo.\n",
        )
    write(layer / "pipelines" / "test.yaml", PIPELINE)
    definitions = DefinitionSet.discover(None, None, layer, allow_repo_pipelines=True)
    return definitions, policy


def run(setup, skills):
    definitions, policy = setup
    return preflight(
        definitions.pipeline("test"),
        definitions,
        policy,
        ResolveContext(team="payments"),
        skills=skills,
    )


def test_a_missing_skill_fails_preflight(setup):
    report = run(setup, Skills(missing={"superpowers/systematic-debugging"}))
    assert not report.passed
    problem = next(p for p in report.problems if p.check == "skills")
    assert problem.step == "develop"
    assert "superpowers/systematic-debugging" in problem.detail
    assert "not installed" in problem.detail


def test_all_skills_present_passes(setup):
    assert not [p for p in run(setup, Skills()).problems if p.check == "skills"]


def test_no_skill_source_given_skips_the_check(setup):
    assert not [p for p in run(setup, None).problems if p.check == "skills"]


def test_more_skills_than_fit_preloaded_is_refused(fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    agent, _ = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    six = [f"acme/skill-{n}" for n in range(MAX_PRELOADED + 1)]
    no_tool = agent.model_copy(update={"skills": six, "tools": ["read_files"]})
    problems = skill_problems(no_tool, Skills())
    assert len(problems) == 1 and f"at most {MAX_PRELOADED}" in problems[0]

    with_tool = no_tool.model_copy(update={"tools": ["read_files", "activate_skill"]})
    assert skill_problems(with_tool, Skills()) == []
    assert len(skill_problems(with_tool, Skills(preload_all=True))) == 1
    assert skill_problems(no_tool.model_copy(update={"skills": six[:5]}), Skills()) == []
