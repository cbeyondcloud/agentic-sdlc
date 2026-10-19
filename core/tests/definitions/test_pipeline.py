"""Tests for loading a pipeline definition (a YAML file of steps, gates and loops)."""

from pathlib import Path

import pytest

from agentic.definitions import (
    ActionStep,
    AgentStep,
    DefinitionError,
    GateStep,
    LoopStep,
    load_pipeline,
)


def write_pipeline(folder: Path, steps_yaml: str) -> Path:
    """Create a pipeline file with a fixed header and the given `steps:` block."""
    path = folder / "pipeline.yaml"
    path.write_text(f"name: test\nversion: 0.1.0\nsteps:\n{steps_yaml}", encoding="utf-8")
    return path


def test_loads_feature_delivery(fixtures: Path):
    pipeline = load_pipeline(fixtures / "pipelines" / "feature-delivery.yaml")

    kinds = [type(step) for step in pipeline.steps]
    assert kinds == [AgentStep, AgentStep, GateStep, LoopStep, GateStep, AgentStep, ActionStep]

    loop = pipeline.steps[3].loop
    assert loop.id == "build-review"
    assert loop.max_iterations == 3
    assert loop.until == "review.open_blocking == 0"
    assert [step.id for step in loop.steps] == ["develop", "review"]

    assert pipeline.steps[1].when == "requirements.size != 'trivial'"
    assert pipeline.steps[2].approver_role == "team-lead"
    assert pipeline.regime == "team"


def test_duplicate_ids_rejected(tmp_path: Path):
    path = write_pipeline(
        tmp_path,
        "  - id: design\n    agent: design\n  - id: design\n    agent: developer\n",
    )

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "duplicate" in str(error.value)
    assert "design" in str(error.value)


def test_nested_loop_rejected(tmp_path: Path):
    inner = (
        "          - loop: {id: inner, max_iterations: 2, until: 'x', steps: [{id: s, agent: a}]}\n"
    )
    path = write_pipeline(
        tmp_path,
        "  - loop:\n      id: outer\n      max_iterations: 2\n      until: 'x'\n"
        "      steps:\n" + inner,
    )

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "loops cannot contain" in str(error.value)


def test_gate_inside_loop_rejected(tmp_path: Path):
    path = write_pipeline(
        tmp_path,
        "  - loop:\n      id: outer\n      max_iterations: 2\n      until: 'x'\n"
        "      steps:\n          - gate: g1\n            approver_role: approver\n",
    )

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "loops cannot contain" in str(error.value)


def test_zero_iterations_rejected(tmp_path: Path):
    path = write_pipeline(
        tmp_path,
        "  - loop:\n      id: l1\n      max_iterations: 0\n      until: 'x'\n"
        "      steps:\n          - id: a\n            agent: developer\n",
    )

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "steps.0.loop.max_iterations" in str(error.value)  # no repeated "loop.loop"


def test_unknown_step_shape_rejected(tmp_path: Path):
    path = write_pipeline(tmp_path, "  - id: ok\n    agent: qa\n  - id: what-is-this\n")

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "steps.1" in str(error.value)


def test_empty_steps_rejected(tmp_path: Path):
    path = write_pipeline(tmp_path, "  []\n")

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "steps" in str(error.value)


def test_broken_yaml_reported_as_definition_error(tmp_path: Path):
    path = write_pipeline(tmp_path, "  - id: [unclosed\n")

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert str(path) in str(error.value)


def test_field_error_names_the_line(tmp_path: Path):
    # name=1, version=2, steps=3, "- loop:"=4, id=5, max_iterations=6
    path = write_pipeline(
        tmp_path,
        "  - loop:\n      id: l1\n      max_iterations: 0\n      until: 'x'\n"
        "      steps:\n          - id: a\n            agent: developer\n",
    )

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "steps.0.loop.max_iterations: Input should be greater than or equal to 1 (line 6)" in (
        str(error.value)
    )


def test_duplicate_key_rejected(tmp_path: Path):
    path = write_pipeline(tmp_path, "  - id: a\n    agent: qa\n    agent: developer\n")

    with pytest.raises(DefinitionError) as error:
        load_pipeline(path)

    assert "duplicate" in str(error.value).lower()


def test_a_loop_with_no_steps_is_refused(tmp_path):  # DF-06
    path = tmp_path / "empty-loop.yaml"
    path.write_text(
        "name: p\nversion: 1.0.0\nsteps:\n  - loop:\n      id: spin\n      max_iterations: 2\n"
        '      until: "review.open_blocking == 0"\n      steps: []\n',
        encoding="utf-8",
    )

    with pytest.raises(DefinitionError, match="steps"):
        load_pipeline(path)
