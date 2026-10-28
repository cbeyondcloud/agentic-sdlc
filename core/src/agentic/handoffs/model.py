"""The parts of a handoff document: envelope (framework fields) + kind data + body."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class HandoffEnvelope(BaseModel):
    """Fields every handoff carries, whatever its kind. Written by the framework."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    schema_version: int
    run_id: str
    step: str
    agent: str  # name@version of the agent that produced it
    produced_at: datetime
    status: Literal["complete", "partial", "blocked"]


@dataclass(frozen=True)
class Handoff:
    envelope: HandoffEnvelope
    data: BaseModel  # one of the models in agentic.handoffs.kinds
    body: str  # the readable Markdown document


class HandoffError(Exception):
    """A handoff document is malformed. One readable line per problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        bullet_list = "\n".join(f"  - {error}" for error in errors)
        super().__init__(f"invalid handoff\n{bullet_list}")
