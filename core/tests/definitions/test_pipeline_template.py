"""Keeps templates/pipeline/pipeline.yaml honest: it documents every field, and every
commented-out example in it is valid if a developer uncomments it."""

import re
from pathlib import Path

from pydantic import BaseModel

from agentic.definitions import (
    ActionStep,
    AgentStep,
    GateStep,
    LoopSpec,
    LoopStep,
    PipelineDefinition,
    load_pipeline,
)

TEMPLATE = Path(__file__).parents[3] / "templates" / "pipeline" / "pipeline.yaml"

# A commented-out setting keeps its indentation before the "#":
#   "# regime: team"            -> "regime: team"
#   "  # - gate: design-approval" -> "  - gate: design-approval"
#   "  #     id: build-review"   -> "      id: build-review"
SAMPLE_LINE = re.compile(r"^(\s*)# (\s*(?:- )?[a-z_]+:.*)$")


def field_names(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


def uncomment(line: str) -> str:
    match = SAMPLE_LINE.match(line)
    return match.group(1) + match.group(2) if match else line


def test_template_loads_as_is():
    pipeline = load_pipeline(TEMPLATE)

    assert pipeline.name == "my-pipeline"
    assert len(pipeline.steps) == 1


def test_template_documents_every_field():
    text = TEMPLATE.read_text(encoding="utf-8")
    all_fields = set().union(
        *(
            field_names(model)
            for model in (PipelineDefinition, AgentStep, GateStep, LoopSpec, LoopStep, ActionStep)
        )
    )

    missing = sorted(name for name in all_fields if f"{name}:" not in text)

    assert missing == [], f"add these fields to {TEMPLATE.name}: {missing}"


def test_every_example_is_valid_when_uncommented(tmp_path: Path):
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    path = tmp_path / "all-examples.yaml"
    path.write_text("\n".join(uncomment(line) for line in lines) + "\n", encoding="utf-8")

    pipeline = load_pipeline(path)  # raises DefinitionError if any example is invalid

    kinds = {type(step) for step in pipeline.steps}
    assert kinds == {AgentStep, GateStep, LoopStep, ActionStep}  # one of each step type
    assert pipeline.regime == "team"
    assert "request" in pipeline.inputs
