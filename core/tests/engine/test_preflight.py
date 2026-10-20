"""Tests for preflight: every check before any spend, with a readable report (M1-25, spec §8.2)."""

from pathlib import Path

import pytest
from _engine_support import AGENTS, write

from agentic.definitions import DefinitionSet
from agentic.engine.preflight import preflight
from agentic.policy import ResolveContext, load_policy
from agentic.sandbox.readiness import SandboxDecision

CLEAN = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
    when: "requirements.size != 'trivial'"
  - gate: design-approval
    approver_role: team-lead
  - loop:
      id: build-review
      max_iterations: 3
      until: "review.open_blocking == 0"
      steps:
        - id: develop
          agent: dev
        - id: review
          agent: reviewer
  - gate: review-approval
    approver_role: approver
    on_reject: develop
"""


@pytest.fixture
def check(tmp_path: Path, fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")

    def run(
        pipeline_yaml: str,
        *,
        extra_agents: dict[str, str] | None = None,
        actions=(),
        type_produces: dict[str, str] | None = None,
        sandbox=None,
        available_backends=None,
    ):
        layer = tmp_path / "layer"
        for name, extra in {**AGENTS, **(extra_agents or {})}.items():
            write(
                layer / "agents" / f"{name}.md",
                f"---\nname: {name}\n{extra}version: 1.0.0\ndescription: {name}\n---\nGo.\n",
            )
        write(layer / "pipelines" / "test.yaml", pipeline_yaml)
        definitions = DefinitionSet.discover(
            org=None, team=None, repo=layer, allow_repo_pipelines=True
        )
        used = policy
        if type_produces:  # an agent TYPE whose produced kind is overridden for the test
            types = {
                name: kind.model_copy(update={"produces": type_produces.get(name, kind.produces)})
                for name, kind in policy.agent_types.items()
            }
            used = policy.model_copy(update={"agent_types": types})
        return preflight(
            definitions.pipeline("test"),
            definitions,
            used,
            ResolveContext(team="payments"),
            actions=actions,
            sandbox=sandbox,
            available_backends=available_backends,
        )

    return run


def checks(report) -> list[str]:
    return [p.check for p in report.problems]


def test_a_clean_pipeline_passes(check):
    report = check(CLEAN)

    assert report.passed, report.text()
    assert report.text().startswith("preflight passed: 4 steps, 2 gates")
    assert set(report.resolved) == {"ba", "designer", "dev", "reviewer"}


def test_unknown_agent(check):
    report = check(CLEAN.replace("agent: reviewer", "agent: ghost"))

    assert not report.passed and "agent" in checks(report)
    assert "review: agent: no agent named 'ghost'" in report.text()


def test_policy_violation_names_the_allowed_value(check):
    report = check(
        CLEAN,
        extra_agents={
            "dev": "type: developer\nconsumes: [design, review]\ntools: [read_files, deploy_prod]\n"
        },
    )

    problem = next(p for p in report.problems if p.check == "policy")
    assert problem.step == "develop" and "tools" in problem.detail
    assert problem.policy_value is not None and "(policy:" in report.text()


def test_a_kind_inherited_from_the_type_must_exist(check):  # DF-44
    # an agent FILE naming an unknown kind is refused at discovery (D37); a kind
    # inherited from the agent TYPE is caught here
    report = check(
        CLEAN + "  - id: notes\n    agent: writer\n",
        extra_agents={"writer": "type: general\n"},
        type_produces={"general": "architecture"},
    )

    problem = next(p for p in report.problems if p.check == "kind" and p.step == "notes")
    assert "architecture" in problem.detail and not report.passed


def test_non_native_backend(check):
    report = check(
        CLEAN,
        extra_agents={"dev": "type: developer\nconsumes: [design, review]\nbackend: claude-code\n"},
    )

    problem = next(p for p in report.problems if p.check == "backend")
    assert problem.step == "develop" and "not available" in problem.detail
    assert report.passed is False  # an unavailable backend blocks


def test_capability_problems_reach_preflight(check):  # M1-27, Review Focus 4
    extra = (
        "type: developer\nconsumes: [design, review]\nbackend: claude-code\n"
        "temperature: 0.2\n"  # the developer type inherits spawn_agent and codeintel
    )
    report = check(CLEAN, extra_agents={"dev": extra}, available_backends={"native", "claude-code"})
    found = [(p.step, p.severity) for p in report.problems if p.check == "backend"]
    assert ("develop", "error") in found  # tools claude-code cannot offer
    assert ("develop", "warning") in found  # temperature not honoured (policy: warn)
    assert any("spawn_agent" in p.detail for p in report.problems if p.check == "backend")


def test_input_from_a_conditional_step_is_a_warning(check):  # DF-05
    report = check(CLEAN)

    warning = next(p for p in report.problems if p.check == "compatibility")
    assert warning.severity == "warning" and "design" in warning.detail
    assert report.passed  # a warning does not fail preflight
    assert "warning" in report.text()


def test_condition_that_does_not_compile(check):
    report = check(CLEAN.replace("review.open_blocking == 0", "review.open_blocking =="))

    problem = next(p for p in report.problems if p.check == "condition")
    assert problem.step == "build-review" and not report.passed


def test_condition_reading_a_kind_no_step_produces(check):
    report = check(CLEAN.replace("requirements.size != 'trivial'", "test_report.verdict == 'pass'"))

    problem = next(p for p in report.problems if p.check == "condition")
    assert problem.step == "design" and "test-report" in problem.detail


def test_a_condition_may_read_a_dashed_kind_by_its_cel_name(check):
    report = check(
        CLEAN.replace(
            'until: "review.open_blocking == 0"',
            'until: "review.open_blocking == 0 && has(dev_report.commits)"',
        )
    )

    assert "condition" not in checks(report), report.text()


def test_on_reject_must_name_an_earlier_step(check):
    report = check(CLEAN.replace("on_reject: develop", "on_reject: qa"))

    problem = next(p for p in report.problems if p.check == "gate")
    assert problem.step == "review-approval" and "qa" in problem.detail


def test_unknown_action(check):
    report = check(CLEAN + "  - id: publish\n    action: github.open_pr\n")

    problem = next(p for p in report.problems if p.check == "action")
    assert problem.step == "publish" and "github.open_pr" in problem.detail
    assert check(
        CLEAN + "  - id: publish\n    action: github.open_pr\n", actions={"github.open_pr"}
    ).passed


# --- the sandbox decision (M1-33, D38) ---


def sandbox_problems(report):
    return [(p.check, p.severity) for p in report.problems if p.check == "sandbox"]


def test_a_sandbox_refusal_fails_preflight(check):
    assert check(CLEAN).passed
    decision = SandboxDecision(False, "none", "the 'regulated' regime needs an OS sandbox", None)
    report = check(CLEAN, sandbox=decision)
    assert not report.passed
    assert sandbox_problems(report) == [("sandbox", "error")]
    assert "regulated" in report.text()


def test_a_sandbox_warning_is_reported_but_passes(check):
    report = check(CLEAN, sandbox=SandboxDecision(False, "none", None, "policy guards only"))
    assert report.passed
    assert sandbox_problems(report) == [("sandbox", "warning")]


def test_a_ready_sandbox_adds_nothing(check):
    report = check(CLEAN, sandbox=SandboxDecision(True, "srt 0.0.78", None, None))
    assert sandbox_problems(report) == []
