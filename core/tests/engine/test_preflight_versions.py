"""Preflight refuses a pipeline missing an org-required step (M2-23, spec §6.1: "preflight
rejects pipelines that drop them"), warns on a deprecated version and refuses one past its
date (M2-15, spec §12.5)."""

from datetime import date
from pathlib import Path

import pytest
from _engine_support import AGENTS, write

from agentic.definitions import DefinitionSet
from agentic.definitions.versions import Deprecation, Deprecations, Pins
from agentic.engine.preflight import preflight
from agentic.policy import ResolveContext, load_policy

PIPELINE = """\
name: test
version: 1.0.0
steps:
  - id: develop
    agent: helper
  - gate: review-approval
    approver_role: approver
"""
SOON = date(2026, 12, 1)


@pytest.fixture
def policy(fixtures: Path):
    return load_policy(fixtures / "policy" / "policy")


@pytest.fixture
def org(tmp_path: Path) -> Path:
    layer = tmp_path / "org"
    # a helper that needs no handoff, so the pipeline is otherwise clean
    for name, extra in {**AGENTS, "helper": "type: general\nconsumes: []\n"}.items():
        write(
            layer / "agents" / f"{name}.md",
            f"---\nname: {name}\n{extra}version: 1.0.0\ndescription: {name}\n---\nGo.\n",
        )
    write(layer / "pipelines" / "test.yaml", PIPELINE)
    return layer


def run(org: Path, policy, today=date(2026, 11, 1), skill_versions=None, **changes):
    used = policy.model_copy(update=changes)
    definitions = DefinitionSet.discover(
        org, None, None, rules=used.rules(team="payments", repo=None, today=today)
    )
    return preflight(
        definitions.pipeline("test"),
        definitions,
        used,
        ResolveContext(team="payments"),
        skill_versions=skill_versions,
    )


def problems(report, check):
    return [p for p in report.problems if p.check == check]


def test_preflight_refuses_missing_required_step(org, policy):
    report = run(org, policy, required_steps={"test": ["review-approval", "security"]})
    assert not report.passed
    [missing] = problems(report, "required-step")
    assert "'security'" in missing.detail and "required_steps" in missing.detail


def test_required_steps_of_other_pipelines_do_not_apply(org, policy):
    report = run(org, policy, required_steps={"other": ["security"]})
    assert problems(report, "required-step") == []


def test_preflight_warns_on_a_deprecated_pipeline(org, policy):
    gone = Deprecations(pipelines={"test": {"1.0.0": Deprecation(blocked_from=SOON)}})
    report = run(org, policy, deprecated=gone)
    [warning] = problems(report, "version")
    assert warning.severity == "warning" and warning.step is None
    assert "refused from 2026-12-01" in warning.detail
    assert report.passed


def test_preflight_blocks_a_deprecated_agent_after_its_date(org, policy):
    gone = Deprecations(agents={"helper": {"1.0.0": Deprecation(blocked_from=SOON, note="use 2")}})
    before = run(org, policy, deprecated=gone, today=date(2026, 11, 30))
    after = run(org, policy, deprecated=gone, today=SOON)
    assert before.passed and problems(before, "version")[0].severity == "warning"
    [blocked] = problems(after, "version")
    assert not after.passed and blocked.step == "develop"
    assert "refused since 2026-12-01 - use 2" in blocked.detail


def test_preflight_checks_deprecated_skills(org, policy):
    """A skill's version is its catalog entry's; the deprecation is policy's."""
    agents = AGENTS["dev"]
    write(
        org / "agents" / "helper.md",
        f"---\nname: helper\n{agents}version: 1.0.0\ndescription: h\n---\nGo.\n",
    )
    gone = Deprecations(
        skills={"superpowers/systematic-debugging": {"1.0.0": Deprecation(blocked_from=SOON)}}
    )
    versions = {"superpowers/systematic-debugging": "1.0.0"}
    late = run(org, policy, deprecated=gone, today=SOON, skill_versions=versions)
    [blocked] = problems(late, "version")
    assert (
        blocked.step == "develop"
        and "skill superpowers/systematic-debugging@1.0.0" in blocked.detail
    )
    newer = run(
        org, policy, deprecated=gone, today=SOON, skill_versions={versions.popitem()[0]: "2.0.0"}
    )
    assert problems(newer, "version") == []


def test_org_policy_rules_carry_the_team_pins(policy):
    teams = {**policy.teams}
    teams["payments"] = teams["payments"].model_copy(update={"pins": Pins(agents={"dev": "0.9.0"})})
    used = policy.model_copy(update={"teams": teams, "required_steps": {"p": ["x"]}})
    rules = used.rules(team="payments", repo="git.example.com/a/b", today=SOON)
    assert rules.pins.agents == {"dev": "0.9.0"} and rules.required_steps == {"p": ["x"]}
    assert (rules.team, rules.repo, rules.today) == ("payments", "git.example.com/a/b", SOON)
    assert used.rules(team="unknown", repo=None, today=SOON).pins == Pins()
