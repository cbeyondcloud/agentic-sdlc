"""An agent step reads the nearest AGENTS.md (as data) and may propose additions in its
handoff; the file itself is never touched (M1-46; spec §10.4)."""

import asyncio
from pathlib import Path

from _engine_support import FakeTrees, answer
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from agentic.agents.runner import PydanticAIRunner

PIPELINE = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
"""
KNOWLEDGE = "Exports go through billing/export.py; never call the bank API directly.\n"


class TreeAt(FakeTrees):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def prepare(self) -> Path:
        super().prepare()
        return self.path


def test_agent_reads_agents_md_and_proposes_without_editing(make_harness, tmp_path):  # RF5
    tree = tmp_path / "tree"
    tree.mkdir()
    agents_md = tree / "AGENTS.md"
    agents_md.write_bytes(KNOWLEDGE.encode("utf-8"))
    before = agents_md.read_bytes()
    reply = answer("design", agents_md_proposals=["Export jobs time out after 30 s."])
    seen: list[str] = []

    def factory(ctx):
        def respond(messages, info):
            seen.append(messages[0].instructions or "")
            return ModelResponse(parts=[TextPart(reply)])

        return PydanticAIRunner(
            ctx.agent,
            FunctionModel(respond),
            budget=ctx.budget,
            state_dir=tmp_path / "state",
            contract=ctx.contract,
            run_id=ctx.run_id,
            stuck_check=lambda messages: None,
        )

    harness = make_harness(PIPELINE, {}, trees_for=lambda run_id: TreeAt(tree))
    harness.engine.runner_factory = factory
    state = asyncio.run(harness.engine.start("test", {"request": "x"}, "dev@x", "r-am"))

    assert state.status == "completed"
    assert KNOWLEDGE.strip() in seen[0]
    assert '<agents-md path="AGENTS.md">' in seen[0]
    handoff = (harness.ledger("r-am").run_dir / state.latest["design"]).read_text("utf-8")
    assert "Export jobs time out after 30 s." in handoff
    assert agents_md.read_bytes() == before  # untouched


def test_no_agents_md_means_no_knowledge_block(make_harness, tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    harness = make_harness(
        PIPELINE, {"designer": [answer("design")]}, trees_for=lambda run_id: TreeAt(tree)
    )
    state = asyncio.run(harness.engine.start("test", {"request": "x"}, "dev@x", "r-am2"))
    assert state.status == "completed"
