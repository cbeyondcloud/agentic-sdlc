"""Unit tests for `SrtSandbox` against a fake `srt` (every OS): how srt is called, and
that stopping the call stops the command (DF-27)."""

import asyncio
import json
import sys
import time
from pathlib import Path

from agentic.sandbox import SandboxRules, SrtSandbox

FAKE_SRT = str(Path(__file__).with_name("fake_srt.py"))


def sandbox(tmp_path: Path) -> SrtSandbox:
    return SrtSandbox(
        SandboxRules(write=(tmp_path,)), tmp_path / "s", srt=(sys.executable, FAKE_SRT)
    )


def test_srt_is_called_with_separate_arguments(tmp_path):
    result = asyncio.run(sandbox(tmp_path).run("echo hi", tmp_path, 30))

    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    settings = tmp_path / "s" / "srt-settings.json"
    tmp = str(tmp_path / "s" / "tmp")
    assert argv == [
        "--settings",
        str(settings),
        "--",
        "env",
        f"TMPDIR={tmp}",
        f"TMP={tmp}",
        f"TEMP={tmp}",
        "bash",
        "-c",
        "echo hi",
    ]
    assert result.exit_code == 0 and "hi" in result.stdout
    assert json.loads(settings.read_text(encoding="utf-8"))["filesystem"]["allowWrite"] == [
        tmp_path.as_posix(),
        Path(tmp).as_posix(),
    ]


def test_the_command_exit_code_comes_back(tmp_path):
    result = asyncio.run(sandbox(tmp_path).run("exit 7", tmp_path, 30))
    assert (result.exit_code, result.timed_out) == (7, False)


def test_a_slow_command_times_out(tmp_path):
    slow = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    started = time.monotonic()
    result = asyncio.run(sandbox(tmp_path).run(slow, tmp_path, 1))
    assert result.timed_out and result.exit_code is None
    assert time.monotonic() - started < 20


def test_cancelling_the_call_stops_the_command(tmp_path):
    late = tmp_path / "late"
    command = f"\"{sys.executable}\" -c \"import time; time.sleep(2); open(r'{late}', 'w')\""

    async def cancel_soon():
        task = asyncio.ensure_future(sandbox(tmp_path).run(command, tmp_path, 60))
        await asyncio.sleep(0.8)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(cancel_soon())
    time.sleep(3)
    assert not late.exists()  # the grandchild died with the call


def test_the_sandbox_is_isolated(tmp_path):
    assert sandbox(tmp_path).isolated is True


def test_temp_files_go_to_a_folder_outside_the_tree(tmp_path):  # review I3
    rules = SandboxRules(write=(tmp_path / "tree",))
    box = SrtSandbox(rules, tmp_path / "s", srt=(sys.executable, FAKE_SRT))
    asyncio.run(box.run("echo hi", tmp_path, 30))
    written = json.loads(box.settings.read_text(encoding="utf-8"))["filesystem"]["allowWrite"]
    assert (tmp_path / "s" / "tmp").as_posix() in written
    assert (tmp_path / "s" / "tmp").is_dir()


def test_a_wrapped_agent_cli_sends_everything_through_the_proxy(tmp_path):
    # srt exempts loopback and private ranges from its proxy (NO_PROXY); inside the
    # sandbox's own network those are unreachable, so a wrapped CLI clears it (M1-29)
    box = sandbox(tmp_path)
    assert "NO_PROXY=" not in box.command_prefix()
    everything = box.command_prefix(proxy_everything=True)
    assert "NO_PROXY=" in everything and "no_proxy=" in everything


def test_the_settings_file_is_never_writable_inside_the_sandbox(tmp_path):  # DF-71
    # a writable path holding the settings would let one command rewrite the next one's rules
    box = sandbox(tmp_path)
    box.command_prefix()
    rules = json.loads(box.settings.read_text(encoding="utf-8"))["filesystem"]
    assert box.settings.as_posix() in rules["denyWrite"]
    assert (tmp_path / "s" / "tmp").as_posix() in rules["allowWrite"]
