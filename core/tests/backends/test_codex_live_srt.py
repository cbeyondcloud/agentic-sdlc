"""The whole Codex process under our srt sandbox (M2-40, DF-114; D38, spike S3 decision 3):
it still reaches TokenLean (the only allowed host), and a command writing outside the
tree is blocked by srt even though Codex's own sandbox is off (`danger-full-access`).
Runs only where srt and the pinned Codex are both installed: the Linux check image."""

import asyncio
import os
import shutil
import subprocess
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from anthropic_stub import RefusingProxy
from conformance import TASK, FakeContract, slice_
from responses_stub import Reply, ResponsesStub, tool_outputs

from agentic.backends.codex import CODEX_VERSION, CodexRunner, CodexSettings, lock_down
from agentic.backends.codex_rules import ShellRules
from agentic.engine.engine import StepContext
from agentic.policy.model import ModelPrice
from agentic.sandbox import SandboxRules, SrtSandbox
from agentic.transport import CallIdentity


def _ready() -> bool:
    if os.name == "nt" or shutil.which("srt") is None or shutil.which("codex") is None:
        return False
    out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30)
    return CODEX_VERSION in out.stdout


pytestmark = pytest.mark.skipif(
    not _ready(),
    reason=f"needs srt and the pinned Codex CLI {CODEX_VERSION} (the Linux check image; on "
    "Windows both run inside WSL2, D38)",
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


def test_codex_runs_under_srt_and_cannot_write_outside_the_tree(resolved, tmp_path):
    stub, proxy = ResponsesStub(), RefusingProxy()
    tree, homes, outside = tmp_path / "tree", tmp_path / "codex-homes", tmp_path / "outside"
    for folder in (tree, homes, outside):
        folder.mkdir()
    stub.queue = [
        Reply(shell=f"touch {outside.as_posix()}/escaped"),
        Reply(shell="touch inside-ok"),
        Reply(text='{"document": "VALID done"}'),
    ]
    agent = resolved.model_copy(
        update={"backend": "codex", "tools": ["read_files", "run_shell"], "effort": None}
    )
    budget = slice_()
    step = StepContext("r-1", "develop", 1, agent, budget, FakeContract())
    settings = CodexSettings(gateway_url=stub.url, api_key="k", home_root=homes, retries=0)
    environ = {**os.environ, "HTTPS_PROXY": proxy.url, "HTTP_PROXY": proxy.url}
    launch = lock_down(
        step,
        settings=settings,
        identity=IDENTITY,
        rules=ShellRules(allow=("touch",)),  # allowed: runs outside Codex's sandbox
        cwd=tree,
        srt_boundary=True,
        environ=environ,
    )
    port = urlparse(stub.url).port
    rules = SandboxRules(write=(tree, homes), allowed_domains=(f"127.0.0.1:{port}",))
    wrap = SrtSandbox(rules, tmp_path / "sandbox").command_prefix(proxy_everything=True)
    runner = CodexRunner(
        agent,
        launch=launch,
        budget=budget,
        price_for=lambda model: ModelPrice(input_per_mtok=Decimal(1), output_per_mtok=Decimal(1)),
        contract=FakeContract(),
        time_limit_s=180,
        wrap=wrap,
    )
    try:
        outcome = asyncio.run(runner.run(TASK))
    finally:
        stub.close()
        proxy.close()
    assert (outcome.status, outcome.output) == ("completed", "VALID done")
    assert not (outside / "escaped").exists()  # srt, not Codex, is the boundary
    assert (tree / "inside-ok").exists()
    escaped = tool_outputs(stub.model_calls()[1])[-1]
    assert "exited with code 1" in escaped or "denied" in escaped.lower()
    assert proxy.attempts == []  # srt's proxy carried the calls; nothing else went out
