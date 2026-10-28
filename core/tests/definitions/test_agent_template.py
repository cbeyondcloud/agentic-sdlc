"""Keeps templates/agent/agent.md honest: it documents every field, and every
commented-out sample value in it is valid if a developer uncomments it."""

import re
from pathlib import Path

from pydantic import BaseModel

from agentic.definitions import AgentDefinition, ChildLimits, DenyRules, Limits, load_agent

TEMPLATE = Path(__file__).parents[3] / "templates" / "agent" / "agent.md"

# A commented-out setting looks like "# key: value" or "#   key: value" (nested).
SAMPLE_LINE = re.compile(r"^# (\s*[a-z_]+:.*)$")


def field_names(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


def test_template_loads_as_is():
    agent = load_agent(TEMPLATE)

    assert agent.name == "my-agent"


def test_template_documents_every_field():
    text = TEMPLATE.read_text(encoding="utf-8")
    all_fields = (
        field_names(AgentDefinition) - {"instructions"}  # instructions = the body
        | field_names(Limits)
        | field_names(ChildLimits)
        | field_names(DenyRules)
    )

    missing = sorted(name for name in all_fields if f"{name}:" not in text)

    assert missing == [], f"add these fields to {TEMPLATE.name}: {missing}"


def test_every_sample_value_is_valid_when_uncommented(tmp_path: Path):
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    uncommented = [match.group(1) if (match := SAMPLE_LINE.match(line)) else line for line in lines]
    path = tmp_path / "all-samples.md"
    path.write_text("\n".join(uncommented) + "\n", encoding="utf-8")

    agent = load_agent(path)  # raises DefinitionError if any sample is invalid

    assert agent.backend == "native"
    assert agent.deny.shell  # the deny samples were picked up
    assert agent.limits.budget_usd is not None
