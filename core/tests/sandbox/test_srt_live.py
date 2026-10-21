"""Live tests against the real `srt` (M1-32; spike S6). They run wherever `srt` is
installed - the Linux CI container and developers' Macs - and are skipped elsewhere."""

import asyncio
import shutil
import sys
import time
from pathlib import Path

import pytest

from agentic.sandbox import SandboxRules, SrtSandbox

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("srt") is None,
    reason="srt runs on macOS and Linux (Windows: inside WSL2, D38) and must be installed",
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    path = tmp_path / "tree"
    path.mkdir()
    return path


def sandbox(tree: Path) -> SrtSandbox:
    rules = SandboxRules(write=(tree,), deny_write=(tree / ".env",), allowed_domains=("pypi.org",))
    return SrtSandbox(rules, tree.parent / "settings")


def run(tree: Path, command: str):
    return asyncio.run(sandbox(tree).run(command, tree, 60))


def test_write_inside_the_tree_is_allowed(tree):
    assert run(tree, "echo ok > inside.txt").exit_code == 0
    assert (tree / "inside.txt").read_text().strip() == "ok"


def test_write_outside_the_tree_is_blocked(tree):
    outside = tree.parent / "outside.txt"
    assert run(tree, f"echo x > '{outside}'").exit_code != 0
    assert not outside.exists()


def test_denied_path_inside_the_tree_is_blocked(tree):
    run(tree, "echo SECRET=1 > .env")
    assert not (tree / ".env").exists()


def test_an_existing_denied_file_cannot_be_changed_or_removed(tree):  # DF-335, DF-71 (a)
    """The ruling put the sandbox's own settings file on denyWrite as a single path: it
    must hold for a file that exists, inside a folder the command may write."""
    guarded = tree / "settings.json"
    guarded.write_text("original", encoding="utf-8")
    box = SrtSandbox(SandboxRules(write=(tree,), deny_write=(guarded,)), tree.parent / "s")

    asyncio.run(box.run(f"echo changed > '{guarded}'", tree, 60))
    asyncio.run(box.run(f"rm -f '{guarded}'; mv '{guarded}' moved.json", tree, 60))

    assert guarded.read_text(encoding="utf-8") == "original"
    assert not (tree / "moved.json").exists()


def test_https_to_an_unlisted_host_is_blocked(tree):
    assert run(tree, "curl -sS -o /dev/null --max-time 20 https://example.com/").exit_code != 0


def test_https_to_an_allowed_host_works(tree):
    result = run(tree, "curl -sS -o /dev/null --max-time 20 https://pypi.org/simple/")
    assert result.exit_code == 0, result.stderr


def test_cancel_kills_the_sandboxed_grandchild(tree):  # Review Focus 1, DF-27
    async def cancel_soon():
        task = asyncio.ensure_future(
            sandbox(tree).run("(sleep 2; echo late > marker) & wait", tree, 60)
        )
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(cancel_soon())
    time.sleep(3)
    assert not (tree / "marker").exists()


def test_the_probe_passes_on_this_machine(tmp_path):  # the S6 cases, as a tool (DF-60/61)
    from agentic.sandbox import probe

    assert probe.main(["--workdir", str(tmp_path / "probe")]) == 0


def test_temp_files_never_land_in_the_tree(tree):  # review I3
    result = run(tree, "mktemp && python3 -c 'import tempfile; print(tempfile.mkdtemp())'")
    assert result.exit_code == 0, result.stderr
    assert all(str(tree) not in line for line in result.stdout.splitlines())
    assert list(tree.iterdir()) == []
