"""Regression tests for the SP7 final review (Sonnet 5.5) of the claude-code backend, each
against the real pinned Claude Code: C1 (the guard's allow-list must hold), I1 (the
user's environment must not reach Claude Code), I3 (an overshoot is recorded), I4 (the
time limit ends what the agent started), I5 (only the tools Claude Code really offers)."""

import asyncio
import time

import pytest
from anthropic_stub import RefusingProxy, Reply, TokenLeanStub
from conformance import TASK, slice_
from test_claude_code_contract import IDENTITY, PRICE

from agentic.backends.claude_code import ClaudeCodeRunner, ClaudeCodeSettings, lock_down
from agentic.engine.engine import StepContext
from agentic.transport import LimitHit

pytest.importorskip("claude_agent_sdk")

PERMISSIONS = {
    "permissions": {
        "allow": ["Bash(echo:*)", "Bash(sleep:*)", "Bash(touch:*)", "Bash(printenv:*)"],
        "ask": ["Bash(npm publish:*)"],
        "deny": ["Bash(git push:*)", "Edit(infra/**)"],
    }
}


@pytest.fixture
def world(tmp_path):
    stub, proxy = TokenLeanStub(), RefusingProxy()
    tree, outside = tmp_path / "tree", tmp_path / "outside"
    tree.mkdir()
    outside.mkdir()
    yield stub, proxy, tree, outside
    stub.close()
    proxy.close()


def make(resolved, world, tmp_path, *, budget=None, time_limit_s=None, tools=None):
    stub, proxy, tree, _ = world
    agent = resolved.model_copy(
        update={
            "backend": "claude-code",
            "tools": tools or ["read_files", "edit_files", "run_shell"],
            "temperature": None,
            "effort": None,
        }
    )
    budget = budget or slice_()
    settings = ClaudeCodeSettings(
        gateway_url=stub.url, api_key="tl-test-key", config_root=tmp_path / "claude-config"
    )
    step = StepContext("r-1", "develop", 1, agent, budget, None)
    options = lock_down(
        step,
        settings=settings,
        identity=IDENTITY,
        permissions=PERMISSIONS,
        cwd=tree,
        edit_tree="edit_files" in agent.tools,
    )
    options["env"].update(
        {"HTTPS_PROXY": proxy.url, "HTTP_PROXY": proxy.url, "NO_PROXY": "127.0.0.1,localhost"}
    )
    return ClaudeCodeRunner(
        agent,
        options=options,
        budget=budget,
        price_for=lambda m: PRICE,
        contract=None,
        time_limit_s=time_limit_s,
    )


def bash(command: str) -> Reply:
    return Reply(tool="Bash", tool_input={"command": command})


def run(runner):
    return asyncio.run(runner.run(TASK))


# ── C1: nothing the guard does not allow runs ────────────────────────────────


def test_only_what_the_rules_allow_runs(resolved, world, tmp_path):
    stub, _, tree, outside = world
    (outside / "secret.txt").write_text("TOP-SECRET-OUTSIDE\n", encoding="utf-8")
    (outside / "victim.txt").write_text("original\n", encoding="utf-8")
    (tree / "mine.txt").write_text("original\n", encoding="utf-8")
    stub.queue = [
        bash("touch unlisted-ran"),  # an allowed-looking but unlisted program... is `touch`
        bash("mkdir made-by-unlisted"),  # not on the allow list
        bash("echo hi && mkdir chained-ran"),  # one allowed, one not
        bash(f"echo x > {outside.as_posix()}/redirected.txt"),  # a write outside the tree
        bash("npm publish"),  # ask-listed: no approval pause here, so refused
        Reply(tool="Read", tool_input={"file_path": str(outside / "secret.txt")}),
        Reply(
            tool="Edit",
            tool_input={
                "file_path": str(outside / "victim.txt"),
                "old_string": "original",
                "new_string": "changed",
            },
        ),
        Reply(
            tool="Edit",
            tool_input={
                "file_path": str(tree / "mine.txt"),
                "old_string": "original",
                "new_string": "changed",
            },
        ),
        bash("echo allowed-ran"),
        Reply(text="done"),
    ]

    assert run(make(resolved, world, tmp_path)).status == "completed"

    seen = "\n".join(call["last_user_text"] for call in stub.model_calls())
    assert (tree / "unlisted-ran").exists()  # `touch` IS on this allow list
    assert not (tree / "made-by-unlisted").exists()
    assert not (tree / "chained-ran").exists()
    assert not (outside / "redirected.txt").exists()
    assert "TOP-SECRET-OUTSIDE" not in seen
    assert (outside / "victim.txt").read_text(encoding="utf-8") == "original\n"
    assert (tree / "mine.txt").read_text(encoding="utf-8") == "changed\n"  # inside the tree
    assert "allowed-ran" in seen


def test_no_tool_is_auto_approved_wholesale(resolved, tmp_path):
    from test_claude_code_options import IDENTITY as ID
    from test_claude_code_options import PERMISSIONS as PERMS
    from test_claude_code_options import SETTINGS, step

    agent = resolved.model_copy(update={"backend": "claude-code"})
    kw = lock_down(
        step(agent), settings=SETTINGS, identity=ID, permissions=PERMS, cwd=tmp_path, edit_tree=True
    )
    assert kw.get("allowed_tools", []) == []


# ── I5: the tools offered are the tools Claude Code really offers ────────────


def test_the_offered_tools_match_the_capability_map(resolved, world, tmp_path):
    stub = world[0]
    stub.queue = [Reply(text="done")]
    run(make(resolved, world, tmp_path))
    assert all(sorted(call["tools"]) == ["Bash", "Edit", "Read"] for call in stub.model_calls())


# ── I1: the user's environment never reaches Claude Code ─────────────────────


def test_a_hostile_environment_cannot_bypass_tokenlean(resolved, world, tmp_path, monkeypatch):
    stub, proxy, _, _ = world
    hostile = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-leak",
        "AWS_REGION": "us-east-1",
        "ANTHROPIC_AUTH_TOKEN": "auth-token-leak",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-leak",
        "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1",  # a parent Claude Code session
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    stub.queue = [bash("printenv AWS_SECRET_ACCESS_KEY"), Reply(text="done")]

    run(make(resolved, world, tmp_path))
    time.sleep(1)

    calls = stub.model_calls()
    assert calls  # the model was called through TokenLean, not Bedrock or Vertex
    assert proxy.attempts == []
    headers = "\n".join(str(call["headers"]) for call in calls)
    assert "auth-token-leak" not in headers and "oauth-token-leak" not in headers
    assert all("authorization" not in call["headers"] for call in calls)
    assert "aws-secret-leak" not in "\n".join(call["last_user_text"] for call in calls)


# ── I3: a spend past the slice is recorded before the stop ───────────────────


def test_an_overshoot_is_recorded_then_stops(resolved, world, tmp_path):
    stub = world[0]
    stub.queue = [Reply(text="done", input_tokens=10_000_000)]
    budget = slice_("0.01")
    runner = make(resolved, world, tmp_path, budget=budget)
    with pytest.raises(LimitHit):
        run(runner)
    assert budget.spent() > budget.limit_usd  # the ledger shows what was really spent
    assert runner.costs.calls


# ── I4: the time limit ends everything the agent started ─────────────────────


def test_the_time_limit_ends_the_agents_commands(resolved, world, tmp_path):
    stub, _, tree, _ = world
    stub.queue = [bash("sleep 10 && touch late-marker"), Reply(text="done")]
    started = time.monotonic()

    outcome = run(make(resolved, world, tmp_path, time_limit_s=4))

    assert (outcome.status, outcome.stopped) == ("stopped", "time")
    assert time.monotonic() - started < 4 + 4  # killed, not waited for
    time.sleep(10)
    assert not (tree / "late-marker").exists()  # the shell command died with the step


def test_adoption_gives_up_with_a_warning_when_the_process_cannot_be_found(
    resolved, world, tmp_path, monkeypatch, caplog
):  # DF-481: an SDK that renamed its private attributes must not poll forever, silently
    import agentic.backends.claude_code as claude_code

    monkeypatch.setattr(claude_code, "ADOPT_DEADLINE_S", 0.2)
    runner = make(resolved, world, tmp_path)
    runner._client = object()  # no `_transport._process`: what a renamed attribute looks like
    with caplog.at_level("WARNING", logger="agentic.backends.claude_code"):
        asyncio.run(asyncio.wait_for(runner._adopt_when_started(), 5))
    assert "outside the step's process scope" in caplog.text
