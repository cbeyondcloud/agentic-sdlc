"""The engine and the run's tree: a checkpoint per agent step, rollback on reject (M1-30;
spec §9.2). The tree itself is `sdlc_commons.worktrees.StepCheckpoints`; here a fake
records what the engine asks of it."""

import asyncio

from _engine_support import FakeTrees, answer
from pydantic_ai import Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agentic.engine.engine import StepContext
from agentic.engine.gates import Approve, Reject, StandaloneAuthorizer

STARTER, LEAD = "dev@acme.example", "lead@acme.example"

TWO = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - id: develop
    agent: dev
"""

GATED = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - id: develop
    agent: dev
  - gate: dev-approval
    approver_role: approver
    on_reject: design
"""


def run(coro):
    return asyncio.run(coro)


def start(h):
    return run(h.engine.start("test", {}, STARTER, "r-1"))


def decide(h, decision):
    return run(
        h.engine.decide(
            "r-1", decision, authorizer=StandaloneAuthorizer(), self_approval=lambda a, s: None
        )
    )


def harness(make_harness, pipeline, script, trees=None):
    trees = trees or FakeTrees()
    return make_harness(pipeline, script, trees_for=lambda run_id: trees), trees


def test_each_agent_step_begins_fresh_and_finishes(make_harness):
    h, trees = harness(
        make_harness, TWO, {"designer": [answer("design")], "dev": [answer("dev-report")]}
    )
    assert start(h).status == "completed"
    assert trees.calls == [
        ("prepare",),
        ("begin", True),
        ("finish", "agentic: design #1 (r-1)"),
        ("begin", True),
        ("finish", "agentic: develop #1 (r-1)"),
    ]


def test_records_carry_base_and_commit(make_harness):
    h, _ = harness(
        make_harness, TWO, {"designer": [answer("design")], "dev": [answer("dev-report")]}
    )
    state = start(h)
    design, develop = state.history
    assert (design.base, design.commit) == ({"app": "c1"}, {"app": "c2"})
    assert (develop.base, develop.commit) == ({"app": "c3"}, {"app": "c4"})


def test_a_failed_step_is_not_committed(make_harness):
    def provider_down(ctx: StepContext) -> str:
        raise RuntimeError("provider down")

    h, trees = harness(make_harness, TWO, {"designer": [provider_down]})
    state = start(h)
    assert state.pending.id == "step-error"
    assert [c[0] for c in trees.calls] == ["prepare", "begin"]
    assert state.history[-1].base == {"app": "c1"} and state.history[-1].commit is None


def test_reject_rolls_back_to_the_target_steps_base(make_harness):
    h, trees = harness(
        make_harness,
        GATED,
        {"designer": [answer("design"), answer("design")], "dev": [answer("dev-report")] * 2},
    )
    start(h)
    design_base = h.store.load("r-1").history[0].base

    decide(h, Reject("dev-approval", LEAD, "redo the design"))

    assert ("rollback", design_base) in trees.calls
    rollback_at = trees.calls.index(("rollback", design_base))
    assert trees.calls[rollback_at + 1] == ("begin", True)  # the design step runs again after it


def test_invalid_output_reject_rolls_back_only_that_attempt(make_harness):
    h, trees = harness(
        make_harness,
        TWO,
        {"designer": ["prose", "prose", answer("design")], "dev": [answer("dev-report")]},
    )
    state = start(h)
    assert state.pending.id == "invalid-output"
    attempt_base = state.history[-1].base

    decide(h, Reject("invalid-output", LEAD, "write the handoff format"))

    assert ("rollback", attempt_base) in trees.calls


def test_approve_does_not_roll_back(make_harness):
    h, trees = harness(
        make_harness, GATED, {"designer": [answer("design")], "dev": [answer("dev-report")]}
    )
    start(h)
    decide(h, Approve("dev-approval", LEAD))
    assert not any(c[0] == "rollback" for c in trees.calls)


class BrokenTrees(FakeTrees):
    def begin(self, *, fresh: bool):
        raise RuntimeError("the tree is locked by another git process")


def test_a_tree_failure_pauses_as_worktree(make_harness):
    h, _ = harness(make_harness, TWO, {"designer": [answer("design")]}, trees=BrokenTrees())
    state = start(h)
    assert (state.status, state.pending.id) == ("paused", "worktree")
    assert "locked by another git process" in state.pending.reason
    assert h.runners.calls == {}  # the step never ran


def test_tool_approval_resume_keeps_the_tree(make_harness, tmp_path):
    def write_file(path: str) -> str:
        """Write a file (needs a person's approval)."""
        return f"wrote {path}"

    h, trees = harness(
        make_harness, TWO, {"designer": [answer("design")], "dev": [answer("dev-report")]}
    )
    h.runners.tools = [Tool(write_file, requires_approval=True)]
    real = h.runners.__call__

    def factory(ctx):
        runner = real(ctx)
        if ctx.step == "design":

            def model(messages, info):
                if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
                    return ModelResponse(parts=[TextPart(answer("design"))])
                return ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt"})])

            runner.model = FunctionModel(model)
        return runner

    h.engine.runner_factory = factory
    state = start(h)
    assert state.pending.kind == "approval"
    assert [c[0] for c in trees.calls] == ["prepare", "begin"]  # no checkpoint while paused

    run(h.engine.resume("r-1", decisions={call: True for call in state.pending.calls}))
    assert ("begin", False) in trees.calls  # resumed in the same tree, nothing discarded


def test_engine_without_trees_is_unchanged(make_harness):
    h = make_harness(TWO, {"designer": [answer("design")], "dev": [answer("dev-report")]})
    state = start(h)
    assert state.status == "completed"
    assert all(r.base is None and r.commit is None for r in state.history)


def test_action_steps_make_no_tree_calls(make_harness):
    actions = "name: test\nversion: 1.0.0\nsteps:\n  - id: publish\n    action: noop\n"
    trees = FakeTrees()
    h = make_harness(
        actions, {}, actions={"noop": lambda state: "done"}, trees_for=lambda run_id: trees
    )
    assert start(h).status == "completed"
    assert trees.calls == []
