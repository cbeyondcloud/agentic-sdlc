"""Spike S8 (throwaway): pause at a gate in one process, resume in another.

Question: can a Pydantic AI run that stops for human approval (a deferred tool)
be saved to disk and finished by a DIFFERENT process - the engine's gate model,
where `agentic approve` runs long after `agentic run` exited?

Method: a deterministic FunctionModel asks to call `write_file`, a tool marked
`requires_approval=True`. Process A runs until the deferred request, saves the
message history as JSON, and exits. Process B (a fresh interpreter) loads it,
approves or denies the call, and finishes. Checks: the tool ran only after
approval and only in B; a denial reaches the model; the state is plain JSON.

Run:  uv run --with "pydantic-ai-slim==2.54.0" python spikes/s8_deferred_resume/spike.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = [
        p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
    ]
    if returns:
        return ModelResponse(parts=[TextPart(f"after the gate: {returns[-1].content}")])
    return ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt", "text": "hello"})])


def build(workdir: Path) -> Agent:
    agent = Agent(FunctionModel(model), output_type=[str, DeferredToolRequests])

    @agent.tool_plain(requires_approval=True)
    def write_file(path: str, text: str) -> str:
        (workdir / path).write_text(text, encoding="utf-8")
        return f"wrote {len(text)} chars to {path}"

    return agent


def pause(workdir: Path, state: Path) -> None:
    result = build(workdir).run_sync("write the file")
    assert isinstance(result.output, DeferredToolRequests), result.output
    state.write_text(json.dumps({
        "messages": json.loads(ModelMessagesTypeAdapter.dump_json(result.all_messages())),
        "pending": [c.tool_call_id for c in result.output.approvals],
        "requests": result.usage.requests,
    }), encoding="utf-8")
    print(
        f"A paused: pending={len(result.output.approvals)} tool ran in A: "
        f"{(workdir / 'out.txt').exists()} state bytes: {state.stat().st_size}"
    )


def resume(workdir: Path, state: Path, decision: str) -> None:
    saved = json.loads(state.read_text(encoding="utf-8"))
    history = ModelMessagesTypeAdapter.validate_python(saved["messages"])
    verdict = True if decision == "approve" else ToolDenied("refused at the design gate")
    results = DeferredToolResults(approvals={call_id: verdict for call_id in saved["pending"]})
    result = build(workdir).run_sync(message_history=history, deferred_tool_results=results)
    print(
        f"B {decision}: output={result.output!r} tool ran: {(workdir / 'out.txt').exists()} "
        f"requests in B: {result.usage.requests}"
    )


def main() -> None:
    for decision in ("approve", "deny"):
        with tempfile.TemporaryDirectory() as tmp:
            workdir, state = Path(tmp), Path(tmp) / "state.json"
            pause(workdir, state)
            # Process B: a fresh interpreter, started only after A finished.
            subprocess.run([sys.executable, __file__, "resume", str(workdir), str(state),
                            decision], check=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "resume":
        resume(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        main()
