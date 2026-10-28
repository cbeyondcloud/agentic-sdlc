"""The same agent in two processes: built by the resume tests and by their subprocess.

Run as a script, it resumes a paused run in a fresh interpreter:
    python _resume_helper.py <fixtures> <state.json> <workdir> <approve|deny reason> <digest>
"""

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentic.agents.runner import PydanticAIRunner
from agentic.definitions import load_agent
from agentic.policy import ResolveContext, load_policy, resolve_agent
from agentic.transport import BudgetLedger


def resolve(fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    agent, violations = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert agent is not None, violations
    return agent


def model(calls: list) -> FunctionModel:
    """Asks to write a file; once the tool answers, reports what it said."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(len(messages))
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(f"after the gate: {returns[-1].content}")])
        return ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt"})])

    return FunctionModel(respond)


def write_tool(workdir: Path) -> Tool:
    def write_file(path: str) -> str:
        """Write a file (needs a human's approval)."""
        (workdir / path).write_text("hello", encoding="utf-8")
        return f"wrote {path}"

    return Tool(write_file, requires_approval=True)


def runner(agent, workdir: Path, state_dir: Path, calls: list, **kwargs) -> PydanticAIRunner:
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal("1"))
    kwargs.setdefault("tools", [write_tool(workdir)])
    return PydanticAIRunner(agent, model(calls), budget=step, state_dir=state_dir, **kwargs)


if __name__ == "__main__":
    fixtures, state, workdir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    decision: bool | str = True if sys.argv[4] == "approve" else sys.argv[4]
    r = runner(resolve(fixtures), workdir, state.parent, [])
    pending = json.loads(state.read_text(encoding="utf-8"))["pending"]
    decisions = {call_id: decision for call_id in pending}
    outcome = asyncio.run(r.resume(state, decisions, sys.argv[5]))
    print(
        json.dumps(
            {"status": outcome.status, "output": outcome.output, "requests": outcome.usage.requests}
        )
    )
