"""The pipeline definition: a YAML file listing the steps to run (spec §6).

A step is one of four shapes, recognised by its key:

    - id: design            agent step   — runs an agent
      agent: design
    - gate: design-approval gate step    — pauses for a human decision
      approver_role: team-lead
    - loop: {...}           loop step    — repeats its inner steps until a condition holds
    - id: publish           action step  — a built-in action, e.g. open a GitHub PR
      action: github.open_pr

Control flow is linear + gates + bounded loops + `when` conditions (D3);
there are no parallel branches at pipeline level.
"""

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

from agentic.definitions.agent import windows_device_name
from agentic.definitions.errors import DefinitionError
from agentic.yamlio import YamlFileError, load_yaml, read_text

# A step, gate or loop id becomes a file name (step logs, handoffs, state files) and a
# commit-trailer value: the same rule as the step logs' plain file name (DF-180).
PLAIN_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}")


def plain_id(value: str) -> str:
    """`value` when it can be an id on every OS; else a ValueError saying why."""
    if not PLAIN_ID.fullmatch(value) or value.endswith("."):
        raise ValueError(
            f"{value[:60]!r} is not a plain name: use letters, digits, '.', '_' and '-' "
            "(at most 100 characters, not starting or ending with '.') - it becomes a file name"
        )
    if windows_device_name(value):
        raise ValueError(f"{value!r} is a Windows device name, so no file can carry it")
    return value


PlainId = Annotated[str, AfterValidator(plain_id)]


class AgentStep(BaseModel):
    """Run one agent. `when` is an optional condition over earlier handoffs."""

    model_config = ConfigDict(extra="forbid")

    id: PlainId
    agent: str
    when: str | None = None


class ActionStep(BaseModel):
    """Run a built-in action (no model call), e.g. `github.open_pr`."""

    model_config = ConfigDict(extra="forbid")

    id: PlainId
    action: str
    when: str | None = None


class GateStep(BaseModel):
    """Pause the run until a person holding `approver_role` approves or rejects."""

    model_config = ConfigDict(extra="forbid")

    gate: PlainId
    approver_role: str
    four_eyes: bool = False  # True: the approver must not be the person who started the run
    on_reject: str | None = None  # the step a rejection routes back to (default: the one before)


class LoopSpec(BaseModel):
    """Repeat `steps` until `until` is true, at most `max_iterations` times."""

    model_config = ConfigDict(extra="forbid")

    id: PlainId
    max_iterations: int = Field(ge=1)
    until: str
    on_exhausted: Literal["gate", "fail"] = "gate"  # what happens if the limit is reached
    steps: list["Step"] = Field(min_length=1)  # an empty loop would spin until max_iterations

    @field_validator("steps")
    @classmethod
    def only_agents_and_actions_inside(cls, steps: list["Step"]) -> list["Step"]:
        for index, step in enumerate(steps):
            if isinstance(step, LoopStep | GateStep):
                raise ValueError(
                    f"loops cannot contain other loops or gates (inner step {index}); "
                    "put gates before or after the loop"
                )
        return steps


class LoopStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loop: LoopSpec


STEP_KINDS = frozenset({"agent", "gate", "loop", "action"})


def step_kind(value: Any) -> str | None:
    """Decide which step model applies, from the step's distinguishing key."""
    if isinstance(value, dict):
        for key in ("loop", "gate", "action", "agent"):
            if key in value:
                return key
        return None  # unknown shape: pydantic reports the custom error below
    return {
        LoopStep: "loop",
        GateStep: "gate",
        ActionStep: "action",
        AgentStep: "agent",
    }.get(type(value))


Step = Annotated[
    Annotated[AgentStep, Tag("agent")]
    | Annotated[GateStep, Tag("gate")]
    | Annotated[LoopStep, Tag("loop")]
    | Annotated[ActionStep, Tag("action")],
    Discriminator(
        step_kind,
        custom_error_type="invalid_step",
        custom_error_message="a step needs one of the keys: agent, gate, loop, action",
    ),
]

LoopSpec.model_rebuild()  # resolve the forward reference to Step


def step_names(step: Step) -> list[str]:
    """Every name a step contributes to the pipeline's namespace."""
    if isinstance(step, AgentStep | ActionStep):
        return [step.id]
    if isinstance(step, GateStep):
        return [step.gate]
    return [step.loop.id] + [name for inner in step.loop.steps for name in step_names(inner)]


class PipelineDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    regime: Literal["solo", "team", "regulated"] = "team"  # gate regime (spec §6.1)
    max_minutes: int | None = Field(default=None, ge=1)  # run wall-clock limit (M1-26)
    # a run reaching its end on a failing review or test report pauses for a person
    # (DF-238); `complete` is for a pipeline whose product is that report (DF-263, DF-269)
    failing_handoff: Literal["pause", "complete"] = "pause"
    inputs: dict[str, dict[str, Any]] = {}
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def names_are_unique(self) -> "PipelineDefinition":
        # Step ids, gate names and loop ids share one namespace, because later
        # steps and `when`/`until` conditions refer to them by name.
        seen: set[str] = set()
        duplicates: list[str] = []
        for step in self.steps:
            for name in step_names(step):
                if name in seen:
                    duplicates.append(name)
                seen.add(name)
        if duplicates:
            raise ValueError(f"duplicate step/gate/loop names: {', '.join(duplicates)}")
        return self


def load_pipeline(path: Path) -> PipelineDefinition:
    """Read a pipeline YAML file and return a validated PipelineDefinition.

    Raises DefinitionError with one readable line per problem.
    """
    try:
        data, lines = load_yaml(read_text(path))
    except YamlFileError as error:
        raise DefinitionError(path, [str(error)]) from error

    try:
        return PipelineDefinition.model_validate(data)
    except ValidationError as error:
        raise DefinitionError.from_validation_error(
            path, error, "pipeline", union_tags=STEP_KINDS, lines=lines
        ) from error
