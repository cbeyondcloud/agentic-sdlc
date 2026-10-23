"""Egress enforced by the real `srt` (M2-42; spec §9.2, D38): a sandbox built from org policy and
the gateway URL reaches the TokenLean host and refuses every other one - and the refused
server never sees the request. Two local HTTP servers stand in for TokenLean and an
unlisted host, so nothing leaves the machine.

They run wherever `srt` is installed - the Linux check container and developers' Macs -
and are skipped elsewhere: on Windows the sandbox is srt inside WSL2 (D38), where the
framework itself runs, so this same test runs there under Linux."""

import asyncio
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentic.policy.model import SandboxPolicy
from agentic.sandbox import SandboxRules, SrtSandbox, run_process
from agentic.sandbox.egress import egress_hosts

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("srt") is None or shutil.which("curl") is None,
    reason="srt runs on macOS and Linux (on Windows inside WSL2, D38) and must be installed, "
    "with curl",
)


class Server:
    """A loopback HTTP server that answers 200 and counts what it was asked."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                server.hits.append(self.path)
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
                return

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._http.server_address[1]}"

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()


@pytest.fixture
def servers():
    gateway, other = Server(), Server()
    yield gateway, other
    gateway.close()
    other.close()


def curl(prefix: list[str], tree: Path, url: str):
    command = f"curl -fsS -o /dev/null --max-time 10 {url}/probe"
    return asyncio.run(run_process([*prefix, "bash", "-c", command], tree, 60))


def test_a_host_outside_the_egress_list_is_refused_inside_srt(tmp_path, servers):
    gateway, other = servers
    tree = tmp_path / "tree"
    tree.mkdir()
    hosts = egress_hosts(SandboxPolicy(allowed_domains=["pypi.org"]), gateway.url)
    box = SrtSandbox(SandboxRules(write=(tree,), allowed_domains=hosts), tmp_path / "s")
    prefix = box.command_prefix(proxy_everything=True)  # as an agent CLI process runs

    reached = curl(prefix, tree, gateway.url)
    refused = curl(prefix, tree, other.url)

    assert reached.exit_code == 0, reached.stderr
    assert gateway.hits == ["/probe"]
    assert refused.exit_code != 0
    assert other.hits == []  # refused by srt's proxy: the request never left the sandbox


def test_shell_commands_without_the_gateway_cannot_reach_it(tmp_path, servers):
    gateway, _ = servers
    tree = tmp_path / "tree"
    tree.mkdir()
    hosts = egress_hosts(SandboxPolicy(allowed_domains=["pypi.org"]))  # no gateway
    box = SrtSandbox(SandboxRules(write=(tree,), allowed_domains=hosts), tmp_path / "s")

    refused = curl(box.command_prefix(proxy_everything=True), tree, gateway.url)

    assert refused.exit_code != 0
    assert gateway.hits == []
