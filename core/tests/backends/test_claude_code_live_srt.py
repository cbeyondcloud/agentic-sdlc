"""The whole Claude Code process under our srt sandbox (M1-29, DF-65; D38): it still reaches
TokenLean (the only allowed host), and a command writing outside the tree is blocked.
Linux and macOS with srt installed only (the Linux CI image has it)."""

import asyncio
import shutil
import sys
from urllib.parse import urlparse

import pytest
from anthropic_stub import Reply, TokenLeanStub
from conformance import TASK, FakeContract, slice_

from agentic.backends.claude_code import ClaudeCodeRunner, ClaudeCodeSettings, lock_down
from agentic.engine.engine import StepContext
from agentic.policy.model import ModelPrice
from agentic.sandbox import SandboxRules, SrtSandbox
from agentic.transport import CallIdentity

pytest.importorskip("claude_agent_sdk")
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("srt") is None,
    reason="srt runs on macOS and Linux (Windows: inside WSL2, D38) and must be installed",
)

IDENTITY = CallIdentity(
    team="payments",
    user="dev@acme.example",
    repo="acme/app",
    pipeline="feature-delivery",
    run_id="r-1",
    step="develop",
    agent="developer@1.2.0",
    policy_version="v1",
)
PRICE = ModelPrice(input_per_mtok=1, output_per_mtok=1)


def test_claude_code_runs_under_srt_and_cannot_write_outside_the_tree(resolved, tmp_path):
    stub = TokenLeanStub()
    tree, config, outside = tmp_path / "tree", tmp_path / "claude-config", tmp_path / "outside"
    tree.mkdir()
    outside.mkdir()
    stub.queue = [
        Reply(tool="Bash", tool_input={"command": f"touch {outside}/escaped"}),
        Reply(tool="Bash", tool_input={"command": "touch inside-ok"}),
        Reply(text="VALID done"),
    ]
    agent = resolved.model_copy(
        update={
            "backend": "claude-code",
            "tools": ["read_files", "run_shell"],
            "temperature": None,
            "effort": None,
        }
    )
    budget = slice_()
    step = StepContext("r-1", "develop", 1, agent, budget, FakeContract())
    settings = ClaudeCodeSettings(gateway_url=stub.url, api_key="k", config_root=config)
    permissions = {"permissions": {"allow": ["Bash(touch:*)"], "ask": [], "deny": []}}
    options = lock_down(
        step,
        settings=settings,
        identity=IDENTITY,
        permissions=permissions,
        cwd=tree,
        edit_tree=False,
    )
    port = urlparse(stub.url).port
    rules = SandboxRules(write=(tree, config), allowed_domains=(f"127.0.0.1:{port}",))
    wrap = SrtSandbox(rules, tmp_path / "sandbox").command_prefix(proxy_everything=True)
    runner = ClaudeCodeRunner(
        agent,
        options=options,
        budget=budget,
        price_for=lambda m: PRICE,
        contract=FakeContract(),
        time_limit_s=120,
        wrap=wrap,
    )
    try:
        outcome = asyncio.run(runner.run(TASK))
    finally:
        stub.close()

    assert outcome.status == "completed", outcome
    assert stub.model_calls()  # TokenLean was reached through the sandbox's proxy
    assert (tree / "inside-ok").exists()
    assert not (outside / "escaped").exists()
