"""The agent-process wrap enforced by the real `srt`, end to end through the CLI's own
recipe (`StepWiring.process_wrap_for`; M2-42, M2-60, DF-550): a process started under it
reaches the gateway and is refused every other host, and the refused server never sees
the request. Two local HTTP servers stand in for TokenLean and an unlisted host, so
nothing leaves the machine.

They run wherever `srt` is installed - the Linux check container and developers' Macs -
and are skipped elsewhere (on Windows the sandbox is srt inside WSL2, D38)."""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest
from test_wiring import SANDBOXED, step, wiring
from test_wiring import agent as agent  # noqa: F401 - a fixture
from test_wiring import repo as repo  # noqa: F401 - a fixture

from agentic.sandbox import run_process

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("srt") is None or shutil.which("curl") is None,
    reason="srt runs on macOS and Linux (on Windows inside WSL2, D38) and must be installed, "
    "with curl",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "tests" / "sandbox"))
from test_egress_live import Server  # noqa: E402 - the core suite's loopback servers


@pytest.fixture
def servers():
    gateway, other = Server(), Server()
    yield gateway, other
    gateway.close()
    other.close()


def curl(prefix: list[str], tree: Path, url: str):
    command = f"curl -fsS -o /dev/null --max-time 10 {url}/probe"
    return asyncio.run(run_process([*prefix, "bash", "-c", command], tree, 60))


def test_a_live_process_wrap_reaches_only_the_gateway(repo, tmp_path, agent, servers):
    gateway, other = servers
    w = wiring(repo, tmp_path, SANDBOXED, allowed_domains=["pypi.org"])
    w.gateway_url = gateway.url
    home = tmp_path / "agent-home"
    home.mkdir()
    prefix = w.process_wrap_for(step(agent), write=(home,))
    tree = w.tools_for(step(agent)).tree

    reached = curl(prefix, tree, gateway.url)
    refused = curl(prefix, tree, other.url)

    assert reached.exit_code == 0, reached.stderr
    assert gateway.hits == ["/probe"]
    assert refused.exit_code != 0
    assert other.hits == []  # refused by srt's proxy: the request never left the sandbox
