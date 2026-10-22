"""The whole OpenCode process under our srt sandbox (M2-57; D38, spike S7 decision 3): it still
reaches TokenLean (the only allowed host), and a command OpenCode's own policy allows but
whose target it cannot see - a write outside the tree inside `sh -c` - is blocked by the
sandbox. srt, not the agent, is the boundary. Linux check container only (srt and the
pinned opencode binary are installed there)."""

import asyncio
import shutil
import subprocess
import sys
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from conformance import TASK, FakeContract, slice_
from openai_stub import OpenAIStub, Reply

from agentic.backends.acp import ShellRules
from agentic.backends.acp.opencode import OPENCODE
from agentic.engine.engine import StepContext
from agentic.policy.model import ModelPrice
from agentic.sandbox import SandboxRules, SrtSandbox
from agentic.transport import CallIdentity

pytest.importorskip("acp")

from agentic.backends.acp.runner import AcpBackend, AcpSettings  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("srt") is None or shutil.which("opencode") is None,
    reason="srt and the pinned opencode binary run only in the Linux check container",
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


def test_opencode_runs_under_srt_and_cannot_write_outside_the_tree(resolved, tmp_path):
    stub = OpenAIStub()
    tree, home_root, outside = tmp_path / "tree", tmp_path / "acp-home", tmp_path / "outside"
    for folder in (tree, home_root, outside):
        folder.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(tree)], check=True)
    stub.queue = [
        Reply(tool="bash", arguments={"command": f"sh -c 'touch {outside}/escaped'"}),
        Reply(tool="bash", arguments={"command": "touch inside-ok"}),
        Reply(text="VALID done"),
    ]
    outcome = run_under_srt(
        resolved, stub, tmp_path, ShellRules(allow=("sh", "touch")), write=(tree, home_root)
    )
    assert outcome.status == "completed", outcome.detail
    assert stub.model_calls(aux=False), "OpenCode never reached the gateway through srt"
    assert (tree / "inside-ok").exists()
    assert not (outside / "escaped").exists()


def test_srt_with_the_guard_s_write_denials_blocks_a_redirect_into_a_protected_path(
    resolved, tmp_path
):  # review F4: OpenCode's edit denies do not cover what an allowed command writes
    stub = OpenAIStub()
    tree, home_root = tmp_path / "tree", tmp_path / "acp-home"
    for folder in (tree, home_root):
        folder.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(tree)], check=True)
    stub.queue = [
        Reply(tool="bash", arguments={"command": "printf '[core]' >> .git/zz_cfg"}),
        Reply(tool="bash", arguments={"command": "printf ok > inside-ok.txt"}),
        Reply(text="VALID done"),
    ]
    # what the M2-41 wiring passes: Guard.write_denials(tree).paths as deny_write
    outcome = run_under_srt(
        resolved,
        stub,
        tmp_path,
        ShellRules(allow=("printf",)),
        write=(tree, home_root),
        deny_write=(tree / ".git",),
    )
    assert outcome.status == "completed", outcome.detail
    assert (tree / "inside-ok.txt").read_text() == "ok"
    assert not (tree / ".git" / "zz_cfg").exists()


def run_under_srt(resolved, stub, tmp_path, shell, *, write, deny_write=()):
    tree, home_root = tmp_path / "tree", tmp_path / "acp-home"
    port = urlparse(stub.url).port
    rules = SandboxRules(write=write, deny_write=deny_write, allowed_domains=(f"127.0.0.1:{port}",))
    wrap = SrtSandbox(rules, tmp_path / "sandbox").command_prefix(proxy_everything=True)
    agent = resolved.model_copy(
        update={
            "backend": "opencode",
            "model": "openai/contract-model",
            "tools": ["read_files", "run_shell"],
            "temperature": None,
            "effort": None,
        }
    )
    backend = AcpBackend(
        OPENCODE,
        AcpSettings(gateway_url=stub.url, api_key="tl-test-key", home_root=home_root),
        identity_for=lambda step: IDENTITY,
        prices=lambda model: ModelPrice(input_per_mtok=Decimal(1), output_per_mtok=Decimal(1)),
        rules_for=lambda step: shell,
        wrap_for=lambda step: wrap,
        time_limit_s=180,
    )
    step = StepContext("r-1", "develop", 1, agent, slice_(), FakeContract())
    runner = backend.runner(step, tool_context=None, state_dir=tree, spawner=None)
    try:
        return asyncio.run(runner.run(TASK))
    finally:
        stub.close()
