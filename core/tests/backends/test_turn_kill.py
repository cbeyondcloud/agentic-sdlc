"""The kill switch at the agent-CLI runners' turn boundaries (M2-19, M2-60, decision 89).

claude-code, codex and the ACP backend cannot save a conversation to resume it, so a
pause switch holds their run at the next step boundary (the engine asks before every
step). A kill must not wait for that: each runner asks the step's `turn_guard` at every
turn boundary its protocol reports - before each prompt or exec, at each model message
(claude-code), at each tool call (codex, ACP) - and a kill raises `RunKilled` with
nothing it started left running. The engine's watchdog still kills a step blocked
between turns (core/tests/engine)."""

import asyncio

import pytest
from conformance import TASK, slice_
from test_processes import gone

from agentic.backends.turns import TurnCheck
from agentic.engine.engine import StepContext
from agentic.killswitch import RUN, RunKilled, Verdict

KILL = Verdict("kill", reason="incident 42", by="admin@acme.example")
PAUSE = Verdict("pause", reason="maintenance", by="admin@acme.example")


class Switch:
    """A scripted turn guard: each call takes the next verdict (the last one repeats)."""

    def __init__(self, *verdicts: Verdict) -> None:
        self.verdicts, self.calls = list(verdicts), 0

    async def __call__(self) -> Verdict:
        self.calls += 1
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]


def run(runner):
    return asyncio.run(runner.run(TASK))


# ── the helper ────────────────────────────────────────────────────────────────


def test_a_kill_raises_and_a_pause_is_only_noted():
    check = TurnCheck(Switch(RUN, PAUSE, KILL))
    asyncio.run(check())
    asyncio.run(check())
    assert check.held == PAUSE
    with pytest.raises(RunKilled) as killed:
        asyncio.run(check())
    assert killed.value.verdict == KILL


def test_without_a_guard_nothing_is_asked():
    asyncio.run(TurnCheck(None)())


# ── codex (fake_codex.py) ─────────────────────────────────────────────────────


@pytest.fixture
def codex(tmp_path, monkeypatch):
    from test_codex_runner import FAKE, IDENTITY, KEY, PRICE, Fake

    import agentic.backends.codex as codex_module

    monkeypatch.setattr(codex_module, "native_windows", lambda: False)
    fake = Fake(tmp_path, monkeypatch)

    def make(agent, guard):
        import sys

        settings = codex_module.CodexSettings(
            gateway_url="http://127.0.0.1:9",
            api_key=KEY,
            home_root=tmp_path / "homes",
            cli=(sys.executable, str(FAKE)),
            require_sandbox=False,
        )
        backend = codex_module.CodexBackend(
            settings,
            identity_for=lambda step: IDENTITY,
            prices=lambda model: PRICE,
            version=lambda cli: codex_module.CODEX_VERSION,
        )
        tree = tmp_path / "tree"
        tree.mkdir(exist_ok=True)
        step = StepContext("r-1", "develop", 1, agent, slice_(), None, guard)
        return backend.runner(step, tool_context=None, state_dir=tree, spawner=None)

    return fake, make


def test_codex_is_killed_at_a_tool_call(resolved, codex, tmp_path):
    from test_codex_runner import agent_for

    fake, make = codex
    fake.set([{"text": "VALID", "tools": 3, "tool_sleep": 0.5, "child": True}])
    switch = Switch(RUN, KILL)  # before the exec, then at the first tool call
    runner = make(agent_for(resolved), switch)
    with pytest.raises(RunKilled):
        run(runner)
    child = int((tmp_path / "codex-log.child").read_text())
    assert gone(child), "the exec's child outlived the kill"
    assert runner.costs.calls, "the model calls made before the kill are charged"


def test_codex_finishes_its_step_under_a_pause(resolved, codex):
    from test_codex_runner import agent_for

    fake, make = codex
    fake.set([{"text": "VALID", "tools": 2}])
    switch = Switch(PAUSE)
    outcome = run(make(agent_for(resolved), switch))
    assert outcome.status == "completed"  # the engine holds the run at the next step
    assert switch.calls >= 3  # before the exec and at each tool call


# ── ACP (acp_fake_agent.py) ───────────────────────────────────────────────────


def acp_runner(resolved, tmp_path, turns, guard):
    pytest.importorskip("acp", reason="the acp extra is not installed")
    from test_acp_runner import World

    world = World(tmp_path, resolved)
    runner = world.runner(turns)
    runner.turn_check = TurnCheck(guard)
    return world, runner


def test_acp_is_killed_at_a_tool_call(resolved, tmp_path):
    turns = [
        [{"spawn": True}, {"tool": "ls", "command": "ls"}, {"sleep": 5}, {"say": "VALID late"}]
    ]
    world, runner = acp_runner(resolved, tmp_path, turns, Switch(RUN, KILL))
    with pytest.raises(RunKilled):
        run(runner)
    [spawned] = world.events("spawned")
    assert gone(spawned["pid"]), "what the agent started outlived the kill"


def test_acp_is_killed_before_its_next_prompt(resolved, tmp_path):
    from conformance import FakeContract

    pytest.importorskip("acp", reason="the acp extra is not installed")
    from test_acp_runner import World

    world = World(tmp_path, resolved)
    runner = world.runner([[{"say": "prose"}], [{"say": "VALID"}]], contract=FakeContract())
    runner.turn_check = TurnCheck(Switch(RUN, KILL))  # the first prompt, then the retry
    with pytest.raises(RunKilled):
        run(runner)
    assert len(world.events("prompt")) == 1


def test_the_acp_backend_passes_the_steps_turn_guard(resolved, tmp_path):
    pytest.importorskip("acp", reason="the acp extra is not installed")
    from decimal import Decimal

    from test_acp_runner import IDENTITY, PRICE, fake_profile

    from agentic.backends.acp.runner import AcpBackend, AcpSettings

    switch = Switch(RUN)
    backend = AcpBackend(
        fake_profile(),
        AcpSettings(gateway_url="https://tl.example", api_key="k", home_root=tmp_path / "h"),
        identity_for=lambda step: IDENTITY,
        prices=lambda model: PRICE,
        fallback_estimate_usd=Decimal("0.01"),
        unsandboxed=True,
    )
    step = StepContext("r-1", "develop", 1, resolved, slice_(), None, switch)
    runner = backend.runner(step, tool_context=None, state_dir=tmp_path, spawner=None)
    assert runner.turn_check.guard is switch


# ── claude-code (the bundled binary against the TokenLean stand-in) ───────────


def test_claude_code_is_killed_at_a_model_message(resolved, tmp_path):
    pytest.importorskip("claude_agent_sdk", reason="the claude-code extra is not installed")
    from anthropic_stub import RefusingProxy, Reply, TokenLeanStub
    from test_claude_code_contract import agent_for, runner_for

    from agentic.backends.processes import process_scope

    stub, proxy = TokenLeanStub(), RefusingProxy()
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        stub.queue = [
            Reply(tool="Bash", tool_input={"command": "echo one"}),
            Reply(tool="Bash", tool_input={"command": "echo two"}),
            Reply(text="VALID"),
        ]
        runner = runner_for(agent_for(resolved), (stub, proxy, tree), tmp_path)
        runner.turn_check = TurnCheck(Switch(RUN, KILL))  # before the query, then a message
        adopted = []
        with process_scope() as scope:
            add = scope.add

            def spy(tree_) -> None:
                adopted.append(tree_.pid)
                add(tree_)

            scope.add = spy
            with pytest.raises(RunKilled):
                run(runner)
        assert len(stub.model_calls()) < 3, "the kill waited for the step to end"
        assert all(gone(pid) for pid in adopted if pid)
    finally:
        stub.close()
        proxy.close()
