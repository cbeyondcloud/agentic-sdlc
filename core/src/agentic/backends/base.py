"""The one protocol every backend implements (M1-28, spec §9.1).

A backend turns one agent step into an `AgentRunner`: the engine drives the runner
the same way whatever drives the model underneath. Backends are provider-agnostic
at this level - what a backend can honour is data in `capabilities.CAPABILITIES`.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentic.agents.runner import AgentRunner
from agentic.agents.tools import ToolContext

if TYPE_CHECKING:
    from agentic.agents.children import ChildSpawner
    from agentic.engine.engine import StepContext


class Backend(Protocol):
    name: str

    def runner(
        self,
        step: "StepContext",
        *,
        tool_context: ToolContext | None,
        state_dir: Path,
        spawner: "ChildSpawner | None",
        is_child: bool = False,
    ) -> AgentRunner: ...
